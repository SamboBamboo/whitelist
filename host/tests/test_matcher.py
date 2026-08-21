"""Matcher daemon (§6): eligibility, verify, drift guard, and the crash-safe
Telegram rule."""

import pytest

from whitelist_host.floodgate import DriftError, FloodgateSettings
from whitelist_host.matcher import Matcher
from whitelist_host.tailer import Tailer

T0 = 1_750_000_000
UUID_B = "00000000-0000-0000-0009-01f64f6dd58e"
FG = FloodgateSettings(prefix=".", replace_spaces=True)

AUTH_B = f"[12:00:00 INFO]: [floodgate] Floodgate player logged in as .Cave_Johnson joined (UUID: {UUID_B})"
KICK_B = "[12:00:02 INFO]: Disconnecting .Cave_Johnson (/192.168.1.50:61000): You are not white-listed on this server!"

SUB = {
    "id": 1,
    "username": "Cave Johnson",
    "real_name": "Jane Doe",
    "platform": "bedrock",
    "normalized": "cave_johnson",
    "normalization_version": 1,
    "created_at": T0 - 600,
    "expires_at": T0 - 600 + 14 * 86400,
}


def write_log(path, *lines):
    with open(path, "a") as f:
        for line in lines:
            f.write(line + "\n")


def make_matcher(tmp_path, conn, worker, telegram, now=T0):
    log = tmp_path / "latest.log"
    log.touch()
    clock = {"t": now}
    m = Matcher(
        conn,
        worker,
        FG,
        Tailer(log, None),
        str(log),
        telegram=telegram,
        window_s=45,
        clock=lambda: clock["t"],
    )
    return m, log, clock


def test_end_to_end_match_verify_notify(tmp_path, conn, worker, telegram):
    worker.pending = [dict(SUB)]
    m, log, _ = make_matcher(tmp_path, conn, worker, telegram)

    write_log(log, AUTH_B, KICK_B)
    stored = m.process_log_batch()
    assert stored == 1
    att = conn.execute("SELECT * FROM attempts").fetchone()
    assert att["outcome"] == "whitelist_rejected"
    assert att["normalized"] == "cave_johnson"  # prefix stripped, lowercased
    assert att["raw_username"] == ".Cave_Johnson"  # exact log token kept
    assert att["uuid"] == UUID_B

    m.sync_with_worker()

    # Verify payload carries the captured identity, never the claimed one.
    assert len(worker.verify_calls) == 1
    call = worker.verify_calls[0]
    assert call["submission_id"] == 1
    assert call["raw_username"] == ".Cave_Johnson"
    assert call["uuid"] == UUID_B
    assert call["attempt_event_id"] == att["event_id"]

    # Attempt linked; notification recorded and sent exactly once.
    assert conn.execute("SELECT submission_id FROM attempts").fetchone()[0] == 1
    note = conn.execute("SELECT * FROM notifications").fetchone()
    assert note["attempt_event_id"] == att["event_id"]
    assert note["telegram_sent_at"] is not None
    assert len(telegram.sent) == 1
    msg = telegram.sent[0]
    assert "Jane Doe" in msg and "Cave Johnson" in msg and UUID_B in msg and "#1" in msg

    # Second sync: nothing new to verify or send.
    m.sync_with_worker()
    assert len(worker.verify_calls) == 1
    assert len(telegram.sent) == 1


def test_crash_between_verify_and_telegram_recovers(tmp_path, conn, worker, telegram):
    """The §6 naive-rule failure: worker transitioned, daemon died before
    sending. The replay (transitioned: false, same event id) plus the
    recently_verified payload must complete the notification."""
    worker.pending = [dict(SUB)]
    m, log, _ = make_matcher(tmp_path, conn, worker, telegram)
    write_log(log, AUTH_B, KICK_B)
    m.process_log_batch()

    # Simulate the crash: verification succeeded at the worker, but Telegram
    # never went out (row exists, unsent).
    telegram.ok = False
    m.sync_with_worker()
    note = conn.execute("SELECT * FROM notifications").fetchone()
    assert note["telegram_sent_at"] is None
    assert note["attempts_made"] == 1
    event_id = note["attempt_event_id"]

    # "Restart": fresh matcher, same DB. The submission is no longer pending;
    # the worker serves it in recently_verified with the stored event id.
    worker.pending = []
    worker.recently_verified = [
        {
            "id": 1,
            "username": "Cave Johnson",
            "real_name": "Jane Doe",
            "platform": "bedrock",
            "normalized": "cave_johnson",
            "uuid": UUID_B,
            "attempt_event_id": event_id,
            "verified_at": T0 + 5,
            "status": "verified",
        }
    ]
    telegram.ok = True
    m2, _, _ = make_matcher(tmp_path, conn, worker, telegram)
    m2.sync_with_worker()

    note = conn.execute("SELECT * FROM notifications").fetchone()
    assert note["telegram_sent_at"] is not None
    assert len(telegram.sent) == 1

    # And the rule is exact-event-keyed: a THIRD sync sends nothing more.
    m2.sync_with_worker()
    assert len(telegram.sent) == 1


def test_old_attempts_never_auto_verify(tmp_path, conn, worker, telegram):
    """§3: attempts older than the 60-minute lookback prove past control,
    not present control."""
    worker.pending = [dict(SUB)]
    m, log, clock = make_matcher(tmp_path, conn, worker, telegram)
    write_log(log, AUTH_B, KICK_B)
    clock["t"] = SUB["created_at"] - 3700  # attempt happened >1h before the form
    m.process_log_batch()
    clock["t"] = T0
    m.sync_with_worker()
    assert worker.verify_calls == []
    assert telegram.sent == []


def test_attempt_first_path_scans_stored_attempts(tmp_path, conn, worker, telegram):
    """§3: 'tried to connect first, then found the form'. The attempt is
    tailed BEFORE the submission exists; a later pending refresh must scan
    stored attempts, not only newly-tailed lines."""
    m, log, clock = make_matcher(tmp_path, conn, worker, telegram)
    clock["t"] = SUB["created_at"] - 1800  # 30 min before the form submission
    write_log(log, AUTH_B, KICK_B)
    m.process_log_batch()
    m.sync_with_worker()  # nothing pending yet
    assert worker.verify_calls == []

    clock["t"] = T0
    worker.pending = [dict(SUB)]
    m.sync_with_worker()
    assert len(worker.verify_calls) == 1
    assert len(telegram.sent) == 1


def test_drift_refuses_to_match(tmp_path, conn, worker, telegram):
    worker.normalization = {"username_prefix": "!", "replace_spaces": True, "version": 1}
    worker.pending = [dict(SUB)]
    m, log, _ = make_matcher(tmp_path, conn, worker, telegram)
    write_log(log, AUTH_B, KICK_B)
    m.process_log_batch()
    with pytest.raises(DriftError):
        m.sync_with_worker()
    assert worker.verify_calls == []


def test_normalization_version_mismatch_skips_row(tmp_path, conn, worker, telegram):
    sub = dict(SUB, normalization_version=2)
    worker.pending = [sub]
    m, log, _ = make_matcher(tmp_path, conn, worker, telegram)
    write_log(log, AUTH_B, KICK_B)
    m.process_log_batch()
    m.sync_with_worker()
    assert worker.verify_calls == []


def test_verify_conflict_is_not_retried_or_notified(tmp_path, conn, worker, telegram):
    worker.pending = [dict(SUB)]
    worker.verify_response = (409, {"error": "conflict", "attempt_event_id": "ev-other"})
    m, log, _ = make_matcher(tmp_path, conn, worker, telegram)
    write_log(log, AUTH_B, KICK_B)
    m.process_log_batch()
    m.sync_with_worker()
    assert len(worker.verify_calls) == 1
    assert telegram.sent == []
    assert conn.execute("SELECT COUNT(*) FROM notifications").fetchone()[0] == 0
    m.sync_with_worker()
    assert len(worker.verify_calls) == 1  # conflicted: not hammered every poll


def test_position_held_back_while_session_open(tmp_path, conn, worker, telegram):
    """A crash between an auth line and its terminal event must replay the
    auth line: the committed offset stays at the open session's start."""
    m, log, _ = make_matcher(tmp_path, conn, worker, telegram)
    write_log(log, AUTH_B)  # session opens, no terminal yet
    m.process_log_batch()
    pos = conn.execute("SELECT * FROM log_position").fetchone()
    assert pos["offset"] == 0  # held at the auth line start

    write_log(log, KICK_B)
    m.process_log_batch()
    pos = conn.execute("SELECT * FROM log_position").fetchone()
    assert pos["offset"] > 0  # session resolved; offset advances
    assert conn.execute("SELECT COUNT(*) FROM attempts").fetchone()[0] == 1


def test_replay_after_crash_does_not_duplicate_attempts(tmp_path, conn, worker, telegram):
    m, log, _ = make_matcher(tmp_path, conn, worker, telegram)
    write_log(log, AUTH_B, KICK_B)
    m.process_log_batch()
    assert conn.execute("SELECT COUNT(*) FROM attempts").fetchone()[0] == 1

    # "Crash" before the position advanced: a fresh matcher re-reads from 0.
    conn.execute("DELETE FROM log_position")
    conn.commit()
    m2, _, _ = make_matcher(tmp_path, conn, worker, telegram)
    m2.tailer = Tailer(log, None)
    m2.process_log_batch()
    assert conn.execute("SELECT COUNT(*) FROM attempts").fetchone()[0] == 1  # UNIQUE event_id
