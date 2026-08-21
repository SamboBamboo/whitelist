-- Whitelist gateway — D1 schema (spec §8).
-- Timestamps are unix epoch seconds (INTEGER) throughout.

CREATE TABLE submissions (
  id                    INTEGER PRIMARY KEY,
  real_name             TEXT,          -- nulled by retention
  email                 TEXT,          -- nulled by retention
  notes                 TEXT,          -- reviewer notes; nulled by retention
  username              TEXT NOT NULL, -- as claimed on the form
  raw_username          TEXT,          -- from the authenticated log event
  normalized            TEXT NOT NULL,
  normalization_version INTEGER NOT NULL,
  platform              TEXT NOT NULL CHECK (platform IN ('java','bedrock')),
  status                TEXT NOT NULL DEFAULT 'pending'
                        CHECK (status IN ('pending','verified','approved','denied','expired')),
  token_hash            TEXT NOT NULL UNIQUE,
  uuid                  TEXT,          -- immutable once set
  xuid                  TEXT,          -- TEXT, never a number (exceeds JS safe integer range)
  attempt_event_id      TEXT,          -- returned on replay; drives Telegram
  attempt_seen_at       INTEGER,
  reviewer              TEXT,
  created_at            INTEGER NOT NULL,
  verified_at           INTEGER,
  decided_at            INTEGER,
  terminal_at           INTEGER        -- set on approved|denied|expired
);

-- One active request per (platform, normalized). Enforced here, not with
-- check-then-insert. D1 is SQLite and supports partial unique indexes.
CREATE UNIQUE INDEX uq_active_submission
  ON submissions(platform, normalized)
  WHERE status IN ('pending', 'verified');

CREATE INDEX idx_sub_status   ON submissions(status);
CREATE INDEX idx_sub_match    ON submissions(platform, normalized);
CREATE INDEX idx_sub_terminal ON submissions(terminal_at);

CREATE TABLE email_events (
  id              INTEGER PRIMARY KEY,
  submission_id   INTEGER NOT NULL,
  kind            TEXT NOT NULL CHECK (kind IN ('receipt','nudge','decision')),
  idempotency_key TEXT NOT NULL UNIQUE,
  state           TEXT NOT NULL CHECK (state IN ('pending','sent','failed')),
  resend_id       TEXT,
  attempts_made   INTEGER NOT NULL DEFAULT 0,
  last_error      TEXT,
  sent_at         INTEGER
);

-- Implementation detail, not part of the §8 domain schema: fixed-window IP
-- rate limiting for the public form. Keys are SHA-256 of the client IP; raw
-- IPs are never stored.
CREATE TABLE rate_limits (
  key          TEXT NOT NULL,
  window_start INTEGER NOT NULL,
  count        INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (key, window_start)
);
