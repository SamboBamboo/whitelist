"""LAN admin app (§7): browser-origin protections and independent backend
enforcement of the approval rule."""

import pytest

from whitelist_host.adminapp import create_app

UUID = "00000000-0000-0000-0009-01f64f6dd58e"
ORIGIN = "http://192.168.1.10"

VERIFIED_SUB = {
    "id": 1,
    "real_name": "Jane Doe",
    "email": "jane@example.com",
    "notes": None,
    "username": "Cave Johnson",
    "raw_username": ".Cave_Johnson",
    "normalized": "cave_johnson",
    "normalization_version": 1,
    "platform": "bedrock",
    "status": "verified",
    "uuid": UUID,
    "xuid": "2535405290989773",
    "attempt_event_id": "ev-1",
    "attempt_seen_at": 1_750_000_100,
    "created_at": 1_750_000_000,
    "verified_at": 1_750_000_150,
    "expires_at": 1_750_000_000 + 30 * 86400,
}

PENDING_SUB = dict(
    VERIFIED_SUB,
    id=2,
    username="Foo_Bar",
    raw_username=None,
    normalized="foo_bar",
    platform="java",
    status="pending",
    uuid=None,
    xuid=None,
    attempt_event_id=None,
    attempt_seen_at=None,
    verified_at=None,
)


@pytest.fixture
def app(tmp_path, worker, backend):
    worker.review_submissions = [dict(VERIFIED_SUB), dict(PENDING_SUB)]
    application = create_app(
        worker=worker,
        backend=backend,
        db_path=str(tmp_path / "state.sqlite"),
        allowed_origins=[ORIGIN],
        reviewer="sam",
        clock=lambda: 1_750_000_500,
    )
    application.testing = True
    return application


@pytest.fixture
def client(app):
    return app.test_client()


def good_headers():
    return {"X-Admin-Request": "1", "Origin": ORIGIN}


def test_review_page_renders(client):
    res = client.get("/")
    assert res.status_code == 200
    html = res.get_data(as_text=True)
    assert "Cave Johnson" in html and ".Cave_Johnson" in html
    assert res.headers["Cache-Control"] == "no-store"


def test_disallowed_host_is_rejected(client):
    res = client.get("/", headers={"Host": "evil.example.com"})
    assert res.status_code == 403


def test_mutation_without_custom_header_is_rejected(client):
    res = client.post(
        "/api/approve", json={"submission_id": 1}, headers={"Origin": ORIGIN}
    )
    assert res.status_code == 403
    assert res.get_json()["error"] == "missing_admin_header"


def test_mutation_without_origin_is_rejected(client):
    res = client.post(
        "/api/approve", json={"submission_id": 1}, headers={"X-Admin-Request": "1"}
    )
    assert res.status_code == 403
    assert res.get_json()["error"] == "origin_rejected"


def test_mutation_with_wrong_origin_is_rejected(client):
    res = client.post(
        "/api/approve",
        json={"submission_id": 1},
        headers={"X-Admin-Request": "1", "Origin": "https://evil.example.com"},
    )
    assert res.status_code == 403


def test_approve_happy_path(client, worker, backend):
    res = client.post("/api/approve", json={"submission_id": 1}, headers=good_headers())
    assert res.status_code == 200
    data = res.get_json()
    assert data["state"] == "complete"
    assert any(e["uuid"] == UUID for e in backend.entries())
    assert worker.decision_calls[0]["decision"] == "approved"
    assert worker.decision_calls[0]["uuid"] == UUID


def test_approve_pending_is_refused_by_backend_not_just_ui(client, worker, backend):
    res = client.post("/api/approve", json={"submission_id": 2}, headers=good_headers())
    assert res.status_code == 409
    assert res.get_json()["error"] == "not_approvable"
    assert backend.add_calls == 0
    assert worker.decision_calls == []


def test_approve_unknown_submission_404s(client):
    res = client.post("/api/approve", json={"submission_id": 99}, headers=good_headers())
    assert res.status_code == 404


def test_deny_pending_works(client, worker, backend):
    res = client.post("/api/deny", json={"submission_id": 2}, headers=good_headers())
    assert res.status_code == 200
    assert res.get_json()["state"] == "complete"
    assert backend.add_calls == 0
    assert worker.decision_calls[0]["decision"] == "denied"


def test_double_decision_reports_in_flight(client, worker):
    client.post("/api/deny", json={"submission_id": 1}, headers=good_headers())
    res = client.post("/api/approve", json={"submission_id": 1}, headers=good_headers())
    assert res.status_code == 409
    assert res.get_json()["error"] == "decision_in_flight"


def test_failed_mutation_reports_honestly(client, worker, backend):
    backend.fail_add = True
    res = client.post("/api/approve", json={"submission_id": 1}, headers=good_headers())
    assert res.status_code == 200  # accepted and queued…
    data = res.get_json()
    assert data["state"] == "requested"  # …but NOT reported as applied
    assert "NOT applied" in data["message"]
    assert worker.decision_calls == []


def test_manual_remove_with_audit(client, tmp_path, backend):
    backend.seed("OldTimer", UUID)
    res = client.post(
        "/api/remove",
        json={"name": "OldTimer", "uuid": UUID, "platform": "java"},
        headers=good_headers(),
    )
    assert res.status_code == 200
    assert backend.entries() == []

    from whitelist_host.db import connect

    conn = connect(tmp_path / "state.sqlite")
    audit = conn.execute("SELECT * FROM audit").fetchall()
    conn.close()
    assert len(audit) == 1
    assert audit[0]["action"] == "manual_remove"
    assert audit[0]["uuid"] == UUID
    assert audit[0]["outbox_id"] is None


def test_attempts_page_renders(client, tmp_path):
    res = client.get("/attempts")
    assert res.status_code == 200


def test_allowlist_page_renders(client, backend):
    backend.seed("Foo_Bar", UUID)
    res = client.get("/allowlist")
    assert res.status_code == 200
    assert "Foo_Bar" in res.get_data(as_text=True)
