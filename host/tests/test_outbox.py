"""Decision outbox (§7): reconcile-not-assume, read-back as the success
signal, the audit written-once rule, and the abandon escape hatch."""

import pytest

from whitelist_host.outbox import DecisionOutbox, OutboxBusy, OutboxError

UUID = "00000000-0000-0000-0009-01f64f6dd58e"

REVIEW_SUB = {
    "id": 1,
    "username": "Cave Johnson",
    "platform": "bedrock",
    "normalized": "cave_johnson",
    "status": "verified",
    "uuid": UUID,
    "raw_username": ".Cave_Johnson",
    "attempt_event_id": "ev-1",
}


def make_outbox(conn, worker, backend):
    worker.review_submissions = [dict(REVIEW_SUB)]
    return DecisionOutbox(conn, worker, backend, default_reviewer="sam")


def request_approve(ob):
    return ob.request(
        1,
        "approved",
        uuid=UUID,
        reviewer="sam",
        notes="met at work",
        raw_username=".Cave_Johnson",
        platform="bedrock",
    )


def test_approve_happy_path(conn, worker, backend):
    ob = make_outbox(conn, worker, backend)
    row = request_approve(ob)
    ob.process(row)

    assert backend.add_calls == 1
    assert any(e["uuid"] == UUID for e in backend.entries())
    final = conn.execute("SELECT * FROM outbox WHERE submission_id = 1").fetchone()
    assert final["state"] == "complete"

    # Writeback carried the reviewer's decision.
    assert worker.decision_calls == [
        {
            "submission_id": 1,
            "decision": "approved",
            "uuid": UUID,
            "reviewer": "sam",
            "notes": "met at work",
        }
    ]

    # Audit written exactly once, enforced by outbox_id UNIQUE.
    audits = conn.execute("SELECT * FROM audit").fetchall()
    assert len(audits) == 1
    assert audits[0]["action"] == "approve"
    ob.process_all(force=True)
    assert conn.execute("SELECT COUNT(*) FROM audit").fetchone()[0] == 1


def test_deny_never_touches_the_server(conn, worker, backend):
    ob = make_outbox(conn, worker, backend)
    row = ob.request(1, "denied", uuid=None, reviewer="sam")
    ob.process(row)
    assert backend.add_calls == 0 and backend.remove_calls == 0
    assert conn.execute("SELECT state FROM outbox").fetchone()[0] == "complete"
    assert worker.decision_calls[0]["decision"] == "denied"


def test_approval_requires_uuid(conn, worker, backend):
    ob = make_outbox(conn, worker, backend)
    with pytest.raises(OutboxError):
        ob.request(1, "approved", uuid=None, reviewer="sam")


def test_one_live_decision_per_submission(conn, worker, backend):
    ob = make_outbox(conn, worker, backend)
    request_approve(ob)
    with pytest.raises(OutboxBusy):
        ob.request(1, "denied", uuid=None, reviewer="sam")


def test_mutation_failure_stays_requested_then_recovers(conn, worker, backend):
    ob = make_outbox(conn, worker, backend)
    backend.fail_add = True
    row = request_approve(ob)
    ob.process(row)
    mid = conn.execute("SELECT * FROM outbox").fetchone()
    assert mid["state"] == "requested"
    assert mid["attempts_made"] == 1
    assert "injected" in mid["last_error"]
    assert worker.decision_calls == []  # no writeback claimed

    backend.fail_add = False
    ob.process_all(force=True)
    assert conn.execute("SELECT state FROM outbox").fetchone()[0] == "complete"


def test_readback_is_the_success_signal(conn, worker, backend):
    """A mutation that 'succeeds' without changing the allowlist must NOT
    advance — read-back decides (§7)."""
    ob = make_outbox(conn, worker, backend)
    backend.drop_add = True
    row = request_approve(ob)
    ob.process(row)
    mid = conn.execute("SELECT * FROM outbox").fetchone()
    assert mid["state"] == "requested"
    assert "read-back" in mid["last_error"]
    assert worker.decision_calls == []


def test_startup_reconcile_does_not_rerun_applied_mutation(conn, worker, backend):
    """Crash after the server mutation but before advancing: reconcile reads
    the actual allowlist and advances WITHOUT re-running the mutation."""
    ob = make_outbox(conn, worker, backend)
    request_approve(ob)
    backend.seed(".Cave_Johnson", UUID)  # the pre-crash mutation landed

    ob2 = DecisionOutbox(conn, worker, backend, default_reviewer="sam")
    ob2.reconcile_startup()

    assert backend.add_calls == 0  # never blindly re-run
    assert conn.execute("SELECT state FROM outbox").fetchone()[0] == "complete"
    assert worker.decision_calls[0]["reviewer"] == "sam"  # default after restart


def test_startup_reconcile_performs_missing_mutation(conn, worker, backend):
    ob = make_outbox(conn, worker, backend)
    request_approve(ob)
    ob2 = DecisionOutbox(conn, worker, backend, default_reviewer="sam")
    ob2.reconcile_startup()
    assert backend.add_calls == 1
    assert conn.execute("SELECT state FROM outbox").fetchone()[0] == "complete"


def test_writeback_conflict_surfaces_and_holds(conn, worker, backend):
    worker.decision_response = (409, {"error": "invalid_state", "status": "denied"})
    ob = make_outbox(conn, worker, backend)
    row = request_approve(ob)
    ob.process(row)
    mid = conn.execute("SELECT * FROM outbox").fetchone()
    assert mid["state"] == "writeback_pending"
    assert "409" in mid["last_error"]
    assert conn.execute("SELECT COUNT(*) FROM audit").fetchone()[0] == 0


def test_abandon_reconciles_audits_and_frees_the_slot(conn, worker, backend):
    """§7: a permanently-failed approve blocks recording a deny; abandon
    reconciles the server, audits, and frees UNIQUE(submission_id)."""
    worker.decision_response = (409, {"error": "conflict"})
    ob = make_outbox(conn, worker, backend)
    row = request_approve(ob)
    ob.process(row)  # server applied, writeback stuck on 409
    assert any(e["uuid"] == UUID for e in backend.entries())

    ob.abandon(1, reviewer="sam", detail="worker says denied elsewhere")

    assert not any(e["uuid"] == UUID for e in backend.entries())  # entry undone
    assert conn.execute("SELECT COUNT(*) FROM outbox").fetchone()[0] == 0
    audits = conn.execute("SELECT * FROM audit ORDER BY id").fetchall()
    assert [a["action"] for a in audits] == ["abandon"]
    assert "worker says denied elsewhere" in audits[0]["detail"]

    # The slot is free: a fresh deny can now be recorded.
    worker.decision_response = (200, {"ok": True})
    row2 = ob.request(1, "denied", uuid=None, reviewer="sam")
    ob.process(row2)
    assert conn.execute("SELECT state FROM outbox").fetchone()[0] == "complete"


def test_abandon_of_completed_decision_refuses(conn, worker, backend):
    ob = make_outbox(conn, worker, backend)
    row = request_approve(ob)
    ob.process(row)
    with pytest.raises(OutboxError):
        ob.abandon(1, reviewer="sam")


def test_interrupted_abandon_resumes_at_startup(conn, worker, backend):
    ob = make_outbox(conn, worker, backend)
    request_approve(ob)
    backend.seed(".Cave_Johnson", UUID)
    # Crash mid-abandon: state flipped but nothing else happened.
    conn.execute("UPDATE outbox SET state = 'abandoned' WHERE submission_id = 1")
    conn.commit()

    ob2 = DecisionOutbox(conn, worker, backend, default_reviewer="sam")
    ob2.reconcile_startup()

    assert not any(e["uuid"] == UUID for e in backend.entries())
    assert conn.execute("SELECT COUNT(*) FROM outbox").fetchone()[0] == 0
    assert conn.execute("SELECT action FROM audit").fetchone()[0] == "abandon"


def test_allowlist_conflict_never_overwrites(conn, worker, backend):
    backend.seed(".Cave_Johnson", "11111111-1111-1111-1111-111111111111")
    ob = make_outbox(conn, worker, backend)
    row = request_approve(ob)
    ob.process(row)
    mid = conn.execute("SELECT * FROM outbox").fetchone()
    assert mid["state"] == "requested"
    assert "conflict" in mid["last_error"].lower()
    # The existing entry is untouched.
    assert backend.entries() == [
        {"name": ".Cave_Johnson", "uuid": "11111111-1111-1111-1111-111111111111"}
    ]
