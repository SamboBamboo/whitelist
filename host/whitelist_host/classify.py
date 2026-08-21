"""Session classification (§3) — not "pair or WARN".

Every authenticated session resolves to exactly one outcome:

  joined              authenticated, then successfully joined — normal,
                      expected, NOT noteworthy
  whitelist_rejected  authenticated, then rejected by the allowlist —
                      the only outcome eligible to verify
  other_rejected      authenticated, then rejected for another reason
                      (ban, full, version, other)
  unresolved          authenticated, no terminal event within the window

WARN fires ONLY on unresolved sessions and unparseable-but-relevant lines.
Every allowlisted player produces an authenticated half followed by a join,
constantly; warning on unpaired authenticated halves would fire on all
normal traffic and train the operator to ignore it. Terminal events with no
open session (e.g. a long-joined player quitting) are silently ignored for
the same reason.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field

from .logparse import LogEvent

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class LineRef:
    """Where an event physically came from — feeds the deterministic
    event_id (crash-replay guard, §6) and the offset hold-back."""

    inode: int
    offset: int  # byte offset of the line start within that inode


@dataclass
class OpenSession:
    key: str
    platform: str
    raw_username: str
    uuid: str | None
    xuid: str | None
    time_str: str
    ref: LineRef
    opened_at: int  # wall clock when the auth event was processed


@dataclass(frozen=True)
class CompletedSession:
    event_id: str
    platform: str
    raw_username: str
    uuid: str | None
    xuid: str | None
    outcome: str
    seen_at: int
    ref: LineRef


def derive_event_id(ref: LineRef, time_str: str, raw_username: str) -> str:
    """Deterministic across crash-replays of the same file bytes (§6):
    (inode, offset, timestamp, username) under the attempts.event_id UNIQUE
    constraint makes re-processing a no-op."""
    material = f"{ref.inode}:{ref.offset}:{time_str}:{raw_username}"
    return hashlib.sha256(material.encode()).hexdigest()


@dataclass
class SessionTracker:
    window_s: int = 45
    open_sessions: dict[str, OpenSession] = field(default_factory=dict)

    def _complete(self, sess: OpenSession, outcome: str, now: int) -> CompletedSession:
        return CompletedSession(
            event_id=derive_event_id(sess.ref, sess.time_str, sess.raw_username),
            platform=sess.platform,
            raw_username=sess.raw_username,
            uuid=sess.uuid,
            xuid=sess.xuid,
            outcome=outcome,
            seen_at=now,
            ref=sess.ref,
        )

    def feed(self, ev: LogEvent, ref: LineRef, now: int) -> list[CompletedSession]:
        completed = self.expire(now)
        key = ev.raw_username
        if ev.kind == "auth":
            prior = self.open_sessions.pop(key, None)
            if prior is not None:
                # Re-auth before the old session resolved: the old one is
                # genuinely unresolved — WARN-worthy per §3.
                logger.warning(
                    "session for %r superseded before resolving; recording unresolved",
                    key,
                )
                completed.append(self._complete(prior, "unresolved", now))
            self.open_sessions[key] = OpenSession(
                key=key,
                platform=ev.platform or "java",
                raw_username=ev.raw_username,
                uuid=ev.uuid,
                xuid=ev.xuid,
                time_str=ev.time_str,
                ref=ref,
                opened_at=now,
            )
        elif ev.kind == "join":
            sess = self.open_sessions.pop(key, None)
            if sess is not None:
                completed.append(self._complete(sess, "joined", now))
            # else: daemon started mid-session or pattern gap — normal noise.
        elif ev.kind == "disconnect":
            sess = self.open_sessions.pop(key, None)
            if sess is not None:
                outcome = (
                    "whitelist_rejected"
                    if ev.reason_class == "whitelist"
                    else "other_rejected"
                )
                completed.append(self._complete(sess, outcome, now))
            # else: an already-joined player leaving — expected, silent.
        return completed

    def expire(self, now: int) -> list[CompletedSession]:
        done: list[CompletedSession] = []
        for key in [
            k
            for k, s in self.open_sessions.items()
            if now - s.opened_at > self.window_s
        ]:
            sess = self.open_sessions.pop(key)
            logger.warning(
                "authenticated session for %r saw no terminal event within %ss; unresolved",
                key,
                self.window_s,
            )
            done.append(self._complete(sess, "unresolved", now))
        return done

    def earliest_open_ref(self) -> LineRef | None:
        """The oldest still-open session's line ref. The tailer's committed
        offset must not advance past this point, so a crash replays the
        unresolved session instead of losing it."""
        if not self.open_sessions:
            return None
        earliest = min(
            self.open_sessions.values(), key=lambda s: (s.opened_at, s.ref.offset)
        )
        return earliest.ref
