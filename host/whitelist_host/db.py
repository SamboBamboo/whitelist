"""Local SQLite state (§8), shared by the matcher daemon and the admin app.

Both processes open short-lived connections against one WAL-mode database.
Timestamps are unix epoch seconds. attempts.event_id is UNIQUE — that is the
crash-replay guard: re-tailing already-processed log lines derives the same
deterministic event id and the insert becomes a no-op.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS attempts (
  id            INTEGER PRIMARY KEY,
  event_id      TEXT NOT NULL UNIQUE,   -- crash-replay guard
  raw_username  TEXT NOT NULL,
  normalized    TEXT NOT NULL,
  platform      TEXT NOT NULL,
  uuid          TEXT NOT NULL,
  xuid          TEXT,
  outcome       TEXT NOT NULL
                CHECK (outcome IN ('joined','whitelist_rejected','other_rejected','unresolved')),
  seen_at       INTEGER NOT NULL,
  submission_id INTEGER
);
CREATE INDEX IF NOT EXISTS idx_att_match ON attempts(platform, normalized, seen_at);

CREATE TABLE IF NOT EXISTS outbox (
  id             INTEGER PRIMARY KEY,
  submission_id  INTEGER NOT NULL UNIQUE,   -- one live decision per submission
  decision       TEXT NOT NULL,
  uuid           TEXT,
  state          TEXT NOT NULL
                 CHECK (state IN ('requested','server_applied','writeback_pending','complete','abandoned')),
  attempts_made  INTEGER NOT NULL DEFAULT 0,
  last_error     TEXT,
  created_at     INTEGER NOT NULL,
  updated_at     INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS audit (
  id            INTEGER PRIMARY KEY,
  outbox_id     INTEGER UNIQUE,   -- "written once" enforced in the database
  submission_id INTEGER,
  action        TEXT NOT NULL,    -- approve|deny|manual_remove|abandon
  uuid          TEXT,
  reviewer      TEXT,
  detail        TEXT,
  at            INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS notifications (
  submission_id    INTEGER PRIMARY KEY,
  attempt_event_id TEXT NOT NULL,
  telegram_sent_at INTEGER,
  attempts_made    INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS log_position (
  path   TEXT PRIMARY KEY,
  inode  INTEGER,
  offset INTEGER
);
"""

ATTEMPT_RETENTION_S = 90 * 86400  # purge attempts older than 90 days


def connect(path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.executescript(SCHEMA)
    return conn


def purge_old_attempts(conn: sqlite3.Connection, now: int) -> int:
    with conn:
        cur = conn.execute(
            "DELETE FROM attempts WHERE seen_at < ?", (now - ATTEMPT_RETENTION_S,)
        )
    return cur.rowcount
