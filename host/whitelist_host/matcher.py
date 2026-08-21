"""Matcher daemon (§6): tails the server log, classifies sessions, matches
qualifying attempts against pending submissions, verifies them at the Worker,
and notifies Telegram crash-safely.

Startup order matters: the drift guard runs BEFORE any matching, and a
mismatch refuses to start (§4) — a silent mismatch produces zero matches and
looks identical to "nobody has applied". The same check re-runs on every
poll, so config drift mid-run also halts matching instead of silently
starving it.
"""

from __future__ import annotations

import logging
import sys
import time

from . import NORMALIZATION_VERSION
from .classify import CompletedSession, LineRef, SessionTracker
from .config import Config, load_config
from .db import connect, purge_old_attempts
from .floodgate import DriftError, FloodgateSettings, check_drift, read_floodgate_config
from .httpjson import TransportError
from .logparse import parse_line
from .normalize import normalize_logged
from .tailer import Position, Tailer, load_position, save_position
from .telegram import TelegramSender, verification_message
from .worker_client import WorkerClient, WorkerError

logger = logging.getLogger(__name__)

ELIGIBILITY_LOOKBACK_S = 3600  # §3: submission.created_at - 60 min


class Matcher:
    def __init__(
        self,
        conn,
        client: WorkerClient,
        floodgate: FloodgateSettings,
        tailer: Tailer,
        log_path: str,
        *,
        telegram: TelegramSender | None = None,
        window_s: int = 45,
        clock=time.time,
    ):
        self.conn = conn
        self.client = client
        self.floodgate = floodgate
        self.tailer = tailer
        self.log_path = str(log_path)
        self.telegram = telegram
        self.tracker = SessionTracker(window_s=window_s)
        self.clock = clock
        # Submissions the Worker rejected with a conflict this run: skipped
        # until restart (or until they leave the pending list) to avoid
        # re-posting a doomed verify every poll.
        self._conflicted: set[int] = set()
        self._version_warned: set[int] = set()

    # ------------------------------------------------------------------ log

    def process_log_batch(self) -> int:
        """Tail → parse → classify → commit attempts + log position in ONE
        transaction (§6 crash safety). Returns number of attempts stored."""
        lines = self.tailer.poll()
        now = int(self.clock())
        completed: list[CompletedSession] = []
        for ln in lines:
            events, missed = parse_line(ln.text)
            if missed:
                logger.warning("unparsed relevant log line: %r", ln.text[:300])
            for ev in events:
                completed.extend(
                    self.tracker.feed(ev, LineRef(ln.inode, ln.offset), now)
                )
        completed.extend(self.tracker.expire(now))

        stored = 0
        with self.conn:
            for cs in completed:
                if self._store_attempt(cs):
                    stored += 1
            pos = self._commit_position()
            if pos is not None:
                save_position(self.conn, self.log_path, pos)
        return stored

    def _commit_position(self) -> Position | None:
        """Hold the committed offset back to the oldest unresolved session's
        line, so a crash replays it instead of losing it. Replay is safe:
        event ids are deterministic and UNIQUE."""
        held = self.tracker.earliest_open_ref()
        if held is not None:
            return Position(inode=held.inode, offset=held.offset)
        return self.tailer.current_end()

    def _store_attempt(self, cs: CompletedSession) -> bool:
        if cs.uuid is None:
            logger.warning(
                "authenticated session for %r completed (%s) without a UUID; "
                "log patterns need adjustment against captured lines — skipping",
                cs.raw_username,
                cs.outcome,
            )
            return False
        norm = normalize_logged(
            cs.platform,
            cs.raw_username,
            prefix=self.floodgate.prefix,
            replace_spaces=self.floodgate.replace_spaces,
        )
        if not norm.ok:
            logger.warning(
                "cannot normalize logged name %r (%s); check Floodgate prefix "
                "config — skipping",
                cs.raw_username,
                norm.error,
            )
            return False
        cur = self.conn.execute(
            """INSERT OR IGNORE INTO attempts
               (event_id, raw_username, normalized, platform, uuid, xuid, outcome, seen_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                cs.event_id,
                cs.raw_username,
                norm.normalized,
                cs.platform,
                cs.uuid,
                cs.xuid,
                cs.outcome,
                cs.seen_at,
            ),
        )
        return cur.rowcount == 1

    # --------------------------------------------------------------- worker

    def sync_with_worker(self) -> None:
        """One poll cycle (§6): drift check, scan stored attempts for every
        pending submission (the attempt-first path — not only freshly tailed
        lines), verify eligible matches, complete unsent notifications."""
        try:
            payload = self.client.get_pending()
        except (WorkerError, TransportError) as e:
            logger.warning("worker poll failed, will retry: %s", e)
            return

        problems = check_drift(payload.get("normalization", {}), self.floodgate)
        if problems:
            for p in problems:
                logger.critical("normalization drift: %s", p)
            raise DriftError(
                "normalization config drifted between Worker, Floodgate, and this "
                "code; refusing to match (§4). Fix the config or run the migration."
            )

        pending = payload.get("pending", [])
        pending_ids = {s["id"] for s in pending}
        self._conflicted &= pending_ids  # forget conflicts for departed rows

        # submission info for notification texts; Worker-confirmed
        # (submission_id → attempt_event_id) pairs this cycle.
        sub_info: dict[int, dict] = {}
        confirmed: dict[int, str] = {}
        for rv in payload.get("recently_verified", []):
            sub_info[rv["id"]] = rv
            if rv.get("attempt_event_id"):
                confirmed[rv["id"]] = rv["attempt_event_id"]

        for sub in pending:
            sub_info.setdefault(sub["id"], sub)
            if sub.get("normalization_version") != NORMALIZATION_VERSION:
                if sub["id"] not in self._version_warned:
                    self._version_warned.add(sub["id"])
                    logger.warning(
                        "submission #%s uses normalization_version %s but this "
                        "code implements %s; leaving it for manual handling",
                        sub["id"],
                        sub.get("normalization_version"),
                        NORMALIZATION_VERSION,
                    )
                continue
            if sub["id"] in self._conflicted:
                continue
            att = self._find_eligible_attempt(sub)
            if att is None:
                continue
            self._verify(sub, att, sub_info, confirmed)

        self._send_unsent_notifications(sub_info, confirmed)

    def _find_eligible_attempt(self, sub: dict):
        """§3 eligibility: whitelist_rejected only, unmatched, within
        [created_at - 60 min, expiry]. Older attempts never auto-verify —
        they prove the account was once controlled, not that it is now."""
        return self.conn.execute(
            """SELECT * FROM attempts
               WHERE platform = ? AND normalized = ?
                 AND outcome = 'whitelist_rejected' AND submission_id IS NULL
                 AND seen_at >= ? AND seen_at <= ?
               ORDER BY seen_at DESC LIMIT 1""",
            (
                sub["platform"],
                sub["normalized"],
                sub["created_at"] - ELIGIBILITY_LOOKBACK_S,
                sub["expires_at"],
            ),
        ).fetchone()

    def _verify(self, sub: dict, att, sub_info: dict, confirmed: dict) -> None:
        payload = {
            "submission_id": sub["id"],
            "platform": att["platform"],
            "normalized": att["normalized"],
            "raw_username": att["raw_username"],
            "uuid": att["uuid"],
            "xuid": att["xuid"],
            "attempt_event_id": att["event_id"],
            "attempt_seen_at": att["seen_at"],
        }
        try:
            status, data = self.client.post_verify(payload)
        except TransportError as e:
            logger.warning("verify for #%s failed to send, will retry: %s", sub["id"], e)
            return
        if status == 200 and data.get("attempt_event_id") == att["event_id"]:
            with self.conn:
                self.conn.execute(
                    "UPDATE attempts SET submission_id = ? WHERE id = ?",
                    (sub["id"], att["id"]),
                )
                self.conn.execute(
                    """INSERT OR IGNORE INTO notifications (submission_id, attempt_event_id)
                       VALUES (?, ?)""",
                    (sub["id"], att["event_id"]),
                )
            confirmed[sub["id"]] = att["event_id"]
            sub_info[sub["id"]] = {**sub, "uuid": att["uuid"]}
            if data.get("transitioned"):
                logger.info("submission #%s verified by attempt %s", sub["id"], att["event_id"][:12])
            else:
                logger.info(
                    "submission #%s was already verified by this attempt (replay)",
                    sub["id"],
                )
        elif status == 409:
            logger.info(
                "verify conflict for #%s (worker holds %s); leaving for the admin",
                sub["id"],
                data.get("attempt_event_id"),
            )
            self._conflicted.add(sub["id"])
        else:
            logger.warning("verify for #%s rejected (%s): %s", sub["id"], status, data)
            self._conflicted.add(sub["id"])

    def _send_unsent_notifications(self, sub_info: dict, confirmed: dict) -> None:
        """§6 crash-safe rule: send Telegram when the Worker has confirmed the
        submission was verified by EXACTLY the attempt_event_id in our local
        notifications row, and that row is still unsent. Worker confirmation
        arrives either from this cycle's verify response or from the
        recently_verified list (a restart recognizing prior work)."""
        if self.telegram is None:
            return
        rows = self.conn.execute(
            "SELECT * FROM notifications WHERE telegram_sent_at IS NULL"
        ).fetchall()
        for row in rows:
            sid = row["submission_id"]
            if confirmed.get(sid) != row["attempt_event_id"]:
                continue
            info = sub_info.get(sid)
            if info is None:
                continue
            ok = self.telegram.send(verification_message(info))
            with self.conn:
                if ok:
                    self.conn.execute(
                        """UPDATE notifications
                           SET telegram_sent_at = ?, attempts_made = attempts_made + 1
                           WHERE submission_id = ?""",
                        (int(self.clock()), sid),
                    )
                else:
                    self.conn.execute(
                        "UPDATE notifications SET attempts_made = attempts_made + 1 "
                        "WHERE submission_id = ?",
                        (sid,),
                    )


# ---------------------------------------------------------------------- main


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    cfg = load_config()
    daemon_token = cfg.secret("DAEMON_TOKEN")
    if not daemon_token:
        logger.critical("DAEMON_TOKEN credential missing (§9); cannot talk to the Worker")
        return 1

    try:
        floodgate = read_floodgate_config(cfg.floodgate_config)
    except DriftError as e:
        logger.critical("%s", e)
        return 1

    client = WorkerClient(cfg.worker_base_url, daemon_token=daemon_token)

    # Startup drift check BEFORE matching anything. Retry transport failures
    # (the Worker being briefly unreachable is not drift), abort on mismatch.
    while True:
        try:
            payload = client.get_pending()
            break
        except (WorkerError, TransportError) as e:
            logger.warning("cannot reach worker for startup drift check: %s", e)
            time.sleep(30)
    problems = check_drift(payload.get("normalization", {}), floodgate)
    if problems:
        for p in problems:
            logger.critical("normalization drift: %s", p)
        logger.critical("refusing to start matching (§4)")
        return 1

    telegram = None
    if cfg.telegram_enabled:
        tg_token = cfg.secret("TELEGRAM_BOT_TOKEN")
        if tg_token and cfg.telegram_chat_id:
            telegram = TelegramSender(tg_token, cfg.telegram_chat_id)
        else:
            logger.warning(
                "telegram enabled but TELEGRAM_BOT_TOKEN / chat_id missing; "
                "notifications disabled"
            )

    conn = connect(cfg.db_path)
    tailer = Tailer(cfg.server_log, load_position(conn, cfg.server_log))
    matcher = Matcher(
        conn,
        client,
        floodgate,
        tailer,
        cfg.server_log,
        telegram=telegram,
        window_s=cfg.session_window_s,
    )

    logger.info(
        "matcher started: log=%s worker=%s prefix=%r replace_spaces=%s norm_v%s",
        cfg.server_log,
        cfg.worker_base_url,
        floodgate.prefix,
        floodgate.replace_spaces,
        NORMALIZATION_VERSION,
    )

    last_sync = 0.0
    last_purge = 0.0
    try:
        while True:
            matcher.process_log_batch()
            now = time.monotonic()
            if now - last_sync >= cfg.poll_interval_s:
                matcher.sync_with_worker()
                last_sync = now
            if now - last_purge >= 86400:
                purged = purge_old_attempts(conn, int(time.time()))
                if purged:
                    logger.info("purged %d attempts older than 90 days", purged)
                last_purge = now
            time.sleep(cfg.tail_interval_s)
    except DriftError as e:
        logger.critical("%s", e)
        return 1
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
