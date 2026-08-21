"""Decision outbox (§7): allowlist mutation and Worker writeback cannot
commit atomically, so reconcile rather than assume.

    requested → server_applied → writeback_pending → complete

Startup reconciliation for rows stuck in 'requested' reads the ACTUAL
allowlist first: expected UUID present → advance; absent → mutate, read
back, advance. The rule is not "never re-run the server mutation" — it is
"never BLINDLY re-run it".

UNIQUE(submission_id) — deliberately not (submission_id, decision): the
looser constraint would let a concurrent approve and deny race, where the
approve mutates the server before the Worker rejects the conflicting
decision. Consequence handled: a permanently-failed approve blocks recording
a deny, so `abandon()` provides the explicit escape — reconcile the actual
allowlist (removing the entry if it landed), write the audit row, and delete
the outbox row to free the constraint for a fresh decision. The audit table
(outbox_id UNIQUE, written once) remains the permanent record.

Read-back is the success signal for every tier (§7).
"""

from __future__ import annotations

import logging
import time

from .allowlist import AllowlistBackend, AllowlistConflict, uuid_present
from .httpjson import TransportError
from .worker_client import WorkerClient, WorkerError

logger = logging.getLogger(__name__)

MAX_BACKOFF_S = 3600
BASE_BACKOFF_S = 30


class OutboxBusy(Exception):
    def __init__(self, row):
        super().__init__(
            f"submission {row['submission_id']} already has a live decision "
            f"({row['decision']}, state {row['state']})"
        )
        self.row = row


class OutboxError(Exception):
    pass


def _backoff(attempts: int) -> int:
    return min(BASE_BACKOFF_S * (2 ** min(attempts, 8)), MAX_BACKOFF_S)


class DecisionOutbox:
    def __init__(
        self,
        conn,
        worker: WorkerClient,
        backend: AllowlistBackend,
        *,
        default_reviewer: str = "admin",
        clock=time.time,
    ):
        self.conn = conn
        self.worker = worker
        self.backend = backend
        self.default_reviewer = default_reviewer
        self.clock = clock
        # Request-time context (reviewer, notes, mutation identity) — memory
        # only. After a crash/restart, _mutation_identity refetches identity
        # from the Worker; reviewer falls back to default_reviewer and notes
        # are lost for the retried writeback (the §8 outbox schema carries
        # neither, and /api/decision treats them as reviewer-supplied).
        self._meta: dict[int, dict] = {}

    # ------------------------------------------------------------- requests

    def request(
        self,
        submission_id: int,
        decision: str,
        *,
        uuid: str | None,
        reviewer: str,
        notes: str | None = None,
        raw_username: str | None = None,
        platform: str | None = None,
    ):
        """Record the decision intent. For approvals the caller passes the
        raw_username/platform it just re-checked against /api/review; they are
        needed for the server mutation and ride in memory via process()."""
        if decision not in ("approved", "denied"):
            raise OutboxError(f"bad decision {decision!r}")
        if decision == "approved" and not uuid:
            raise OutboxError("approval without a stored UUID is not allowed (§5)")
        now = int(self.clock())
        try:
            with self.conn:
                cur = self.conn.execute(
                    """INSERT INTO outbox
                       (submission_id, decision, uuid, state, created_at, updated_at)
                       VALUES (?, ?, ?, 'requested', ?, ?)""",
                    (submission_id, decision, uuid, now, now),
                )
        except Exception as e:
            if "UNIQUE" in str(e):
                row = self.conn.execute(
                    "SELECT * FROM outbox WHERE submission_id = ?", (submission_id,)
                ).fetchone()
                raise OutboxBusy(row) from e
            raise
        self._meta[submission_id] = {
            "reviewer": reviewer,
            "notes": notes,
            "raw_username": raw_username,
            "platform": platform,
        }
        return self.conn.execute(
            "SELECT * FROM outbox WHERE id = ?", (cur.lastrowid,)
        ).fetchone()

    # ----------------------------------------------------------- processing

    def process_all(self, *, force: bool = False) -> None:
        rows = self.conn.execute(
            "SELECT * FROM outbox WHERE state IN "
            "('requested','server_applied','writeback_pending')"
        ).fetchall()
        now = int(self.clock())
        for row in rows:
            if not force and row["attempts_made"] > 0:
                due = row["updated_at"] + _backoff(row["attempts_made"])
                if now < due:
                    continue
            self.process(row)

    def process(self, row) -> None:
        sid = row["submission_id"]
        try:
            state = row["state"]
            if state == "requested":
                if row["decision"] == "approved":
                    self._ensure_on_allowlist(sid, row["uuid"])
                self._advance(sid, "server_applied")
                state = "server_applied"
            if state == "server_applied":
                self._advance(sid, "writeback_pending")
                state = "writeback_pending"
            if state == "writeback_pending":
                self._writeback(sid, row)
        except AllowlistConflict as e:
            # Permanent until a human resolves it: surface loudly, never
            # silently overwrite.
            self._record_error(sid, f"conflict: {e}")
            logger.error("outbox #%s allowlist conflict: %s", sid, e)
        except Exception as e:
            self._record_error(sid, str(e))
            logger.warning("outbox #%s attempt failed: %s", sid, e)

    def _ensure_on_allowlist(self, sid: int, uuid: str) -> None:
        """Reconcile-then-mutate (§7): check actual state, mutate only if
        absent, and require read-back confirmation either way."""
        entries = self.backend.entries()
        if not uuid_present(entries, uuid):
            name, platform = self._mutation_identity(sid)
            self.backend.add(name, uuid, platform)
            entries = self.backend.entries()
            if not uuid_present(entries, uuid):
                raise OutboxError(
                    f"allowlist add for {uuid} did not survive read-back; "
                    "NOT reporting success (§7)"
                )

    def _mutation_identity(self, sid: int) -> tuple[str, str]:
        """raw_username + platform for the server mutation. Prefer what the
        admin app passed at request time; refetch from the Worker (the
        authority on verified rows) after a restart."""
        meta = self._meta.get(sid)
        if meta and meta.get("raw_username") and meta.get("platform"):
            return meta["raw_username"], meta["platform"]
        review = self.worker.get_review()
        for sub in review.get("submissions", []) + review.get("recent_terminal", []):
            if sub.get("id") == sid:
                raw = sub.get("raw_username")
                platform = sub.get("platform")
                if raw and platform:
                    return raw, platform
        raise OutboxError(
            f"cannot determine raw_username/platform for submission {sid}; "
            "worker no longer serves it"
        )

    def _writeback(self, sid: int, row) -> None:
        meta = self._meta.get(sid, {})
        payload = {
            "submission_id": sid,
            "decision": row["decision"],
            "uuid": row["uuid"],
            "reviewer": meta.get("reviewer") or self.default_reviewer,
            "notes": meta.get("notes"),
        }
        status, data = self.worker.post_decision(payload)
        if status == 200:
            now = int(self.clock())
            with self.conn:
                self.conn.execute(
                    "UPDATE outbox SET state = 'complete', updated_at = ?, "
                    "last_error = NULL WHERE submission_id = ?",
                    (now, sid),
                )
                # Audit: written once per outbox row, enforced by the DB.
                self.conn.execute(
                    """INSERT OR IGNORE INTO audit
                       (outbox_id, submission_id, action, uuid, reviewer, detail, at)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (
                        row["id"],
                        sid,
                        "approve" if row["decision"] == "approved" else "deny",
                        row["uuid"],
                        payload["reviewer"],
                        "decision written back to worker",
                        now,
                    ),
                )
            logger.info("outbox #%s complete (%s)", sid, row["decision"])
        elif status == 409:
            # The Worker refused: conflicting decision or state. Do NOT
            # advance; a human resolves via abandon or by accepting reality.
            self._record_error(sid, f"worker 409: {data}")
            logger.error("outbox #%s writeback conflict: %s", sid, data)
        else:
            self._record_error(sid, f"worker {status}: {data}")

    # -------------------------------------------------------------- startup

    def reconcile_startup(self) -> None:
        """§7 startup reconciliation. Never blindly re-run a mutation:
        read the actual allowlist first."""
        for row in self.conn.execute(
            "SELECT * FROM outbox WHERE state = 'requested' AND decision = 'approved'"
        ).fetchall():
            try:
                entries = self.backend.entries()
            except Exception as e:
                logger.warning("startup reconcile: cannot read allowlist: %s", e)
                break
            if row["uuid"] and uuid_present(entries, row["uuid"]):
                # The mutation happened before the crash; just advance.
                self._advance(row["submission_id"], "server_applied")
                logger.info(
                    "startup reconcile: #%s already on allowlist, advanced",
                    row["submission_id"],
                )
        # Interrupted abandons: the audit row is the marker of completion.
        for row in self.conn.execute(
            "SELECT * FROM outbox WHERE state = 'abandoned'"
        ).fetchall():
            self._finish_abandon(row, reviewer="startup-reconcile", detail="resumed after restart")
        self.process_all(force=True)

    # -------------------------------------------------------------- abandon

    def abandon(self, submission_id: int, *, reviewer: str, detail: str = "") -> None:
        """Explicit escape hatch (§7): reconcile actual allowlist state,
        remove the entry if present, audit, then delete the row so a fresh
        decision can be recorded. The constraint is never relaxed."""
        row = self.conn.execute(
            "SELECT * FROM outbox WHERE submission_id = ?", (submission_id,)
        ).fetchone()
        if row is None:
            raise OutboxError(f"no outbox row for submission {submission_id}")
        if row["state"] == "complete":
            raise OutboxError(
                f"submission {submission_id} completed; nothing to abandon"
            )
        with self.conn:
            self.conn.execute(
                "UPDATE outbox SET state = 'abandoned', updated_at = ? "
                "WHERE submission_id = ?",
                (int(self.clock()), submission_id),
            )
        row = self.conn.execute(
            "SELECT * FROM outbox WHERE submission_id = ?", (submission_id,)
        ).fetchone()
        self._finish_abandon(row, reviewer=reviewer, detail=detail)

    def _finish_abandon(self, row, *, reviewer: str, detail: str) -> None:
        sid = row["submission_id"]
        undone = ""
        if row["decision"] == "approved" and row["uuid"]:
            entries = self.backend.entries()
            if uuid_present(entries, row["uuid"]):
                name, platform = self._mutation_identity_best_effort(sid, row["uuid"])
                self.backend.remove(name, row["uuid"], platform)
                if uuid_present(self.backend.entries(), row["uuid"]):
                    raise OutboxError(
                        f"abandon: removal of {row['uuid']} did not survive "
                        "read-back; row left in 'abandoned' for retry"
                    )
                undone = "; server entry removed"
        now = int(self.clock())
        with self.conn:
            self.conn.execute(
                """INSERT OR IGNORE INTO audit
                   (outbox_id, submission_id, action, uuid, reviewer, detail, at)
                   VALUES (?, ?, 'abandon', ?, ?, ?, ?)""",
                (
                    row["id"],
                    sid,
                    row["uuid"],
                    reviewer,
                    f"abandoned {row['decision']} in state {row['state']}"
                    f" ({row['last_error'] or 'no error recorded'}){undone}"
                    + (f"; {detail}" if detail else ""),
                    now,
                ),
            )
            # Free UNIQUE(submission_id) for a fresh decision; the audit row
            # is the permanent record.
            self.conn.execute("DELETE FROM outbox WHERE submission_id = ?", (sid,))
        logger.info("outbox #%s abandoned%s", sid, undone)

    def _mutation_identity_best_effort(self, sid: int, uuid: str) -> tuple[str, str]:
        try:
            return self._mutation_identity(sid)
        except OutboxError:
            # Fall back to the allowlist's own name for this UUID.
            from .allowlist import canon_uuid

            for e in self.backend.entries():
                if canon_uuid(str(e.get("uuid", ""))) == canon_uuid(uuid):
                    return str(e.get("name", "")), "java"
            return "", "java"

    # -------------------------------------------------------------- helpers

    def _advance(self, sid: int, state: str) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE outbox SET state = ?, updated_at = ? WHERE submission_id = ?",
                (state, int(self.clock()), sid),
            )

    def _record_error(self, sid: int, error: str) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE outbox SET attempts_made = attempts_made + 1, "
                "last_error = ?, updated_at = ? WHERE submission_id = ?",
                (error[:500], int(self.clock()), sid),
            )
