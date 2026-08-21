# Host half — matcher daemon + LAN admin app

Everything that runs on the Minecraft machine. Talks to the **Worker API
only** — no D1 token exists here (§2).

```
whitelist_host/
  matcher.py      daemon: tail → classify → match → verify → Telegram (§6)
  logparse.py     log line → events (patterns; validate via host/captured/)
  classify.py     session state machine → one outcome per session (§3)
  tailer.py       rotation-safe tail with replayable positions
  normalize.py    §4 rules (same fixtures as the Worker)
  floodgate.py    reads the REAL Floodgate config; drift guard
  worker_client.py / httpjson.py   Worker API client (stdlib urllib)
  telegram.py     at-least-once notification transport
  adminapp.py     LAN Flask app (§7)
  outbox.py       decision outbox: requested → server_applied →
                  writeback_pending → complete (+ abandon escape hatch)
  allowlist.py    Tier 1/2/3 allowlist backends; read-back is success
  mgmt.py         Management Protocol client (Tier 1)
  rcon.py         minimal RCON client (Tier 2)
  probe.py        §0 capability probe CLI
```

## Install

```sh
python3 -m venv /opt/whitelist-gateway/venv
/opt/whitelist-gateway/venv/bin/pip install /path/to/repo/host[management]
mkdir -p /etc/whitelist-gateway/secrets /var/lib/whitelist-gateway
cp host/config.example.toml /etc/whitelist-gateway/config.toml   # then edit
# one secret per 0600 file under /etc/whitelist-gateway/secrets/ (§9)
chown -R whitelist:whitelist /var/lib/whitelist-gateway /etc/whitelist-gateway/secrets
cp host/deploy/*.service /etc/systemd/system/
cp host/deploy/nginx-admin.conf /etc/nginx/conf.d/whitelist-admin.conf  # edit addresses
systemctl daemon-reload
systemctl enable --now whitelist-matcher whitelist-admin
nginx -t && systemctl reload nginx
```

## Before trusting it (§0, in order)

1. `whitelist-probe checklist` — the manual security invariants
   (online-mode, Floodgate auth, loopback-only management/RCON, grey-cloud
   DNS). Non-negotiable: the ownership proof rests on the first two.
2. Capture real log lines per `host/captured/README.md` and run
   `python -m whitelist_host.logparse check host/captured/*.log` until all
   four scenarios parse. **The shipped patterns are defaults, not truth.**
3. If the server has the Management Protocol:
   `whitelist-probe discover` then `whitelist-probe add-test --keep …` with a
   captured profile, then have the real account join (the step that
   matters), for BOTH platforms. Only then set
   `[allowlist] backend = "management"`. Otherwise pick `rcon` or `file`.

## Behavior notes

- The daemon **refuses to start** (exit 1, systemd restart-loops slowly)
  when the Worker's pinned normalization config disagrees with the real
  Floodgate config or this code's `NORMALIZATION_VERSION` (§4). The same
  check re-runs on every poll. Changing prefix/replace-spaces/version is a
  migration, not a config edit.
- Eligibility (§3): only `whitelist_rejected` attempts inside
  `[created_at − 60 min, expiry]` verify — enforced here AND re-checked by
  the Worker. Older attempts stay visible in the admin UI, matched to
  nothing.
- Telegram is at-least-once (§6): the notification row is keyed by the
  exact `attempt_event_id` the Worker confirmed, so a crash between verify
  and send is completed after restart; an ambiguous timeout can produce a
  rare duplicate ping, never a silent miss. Unsent notifications retry every
  poll cycle (~60 s).
- The admin app reports outbox state honestly (§7): "Approved on server;
  writeback pending" is exactly that; a failed mutation or read-back is
  never reported as success. A stuck decision offers Retry and Abandon —
  abandon reconciles the server allowlist, writes the audit row, and frees
  the one-live-decision slot.

## Tests

```sh
cd host && python -m pytest
```

Covers the shared §4 fixtures, session classification (§3), tailer rotation
and replay, matcher eligibility + crash-safe Telegram, outbox reconciliation
+ abandon, the file-tier write ritual, and the admin app's origin guards.
