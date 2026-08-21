# Minecraft Whitelist Gateway

Self-service whitelist requests for `mine.sambonius.net` (Paper +
Geyser/Floodgate). An applicant fills out a form, then **deliberately
attempts to connect and is rejected** — the server logs the attempt with the
account's authenticated identity and UUID, proving the applicant controls
the account *now*. A human approves from the LAN.

Built to `whitelistgatewayspec.md` v3. Honest framing (§1): the time-bounded
pairing is strong temporal evidence, not cryptographic proof, that submitter
and player are the same person — user-facing copy doesn't oversell it.

## The two halves

Approval requires two halves arriving by different paths — a **submission**
(Cloudflare D1) and a qualifying **attempt** (local server log) — meeting on
the Minecraft host. The repo splits the same way:

```
                     ┌───────────────────────────────────┐
      applicant ───► │ worker/   whitelist.sambonius.net │  TypeScript
                     │ public form + API + D1 + cron     │  Cloudflare
                     └───────────────┬───────────────────┘
                             ▲       │ GET /api/pending, /api/review
                 POST verify │       ▼
                 POST decision       │
   ┌─────────────────────── Minecraft host ───────────────────────┐
   │ host/    matcher daemon ──► tails latest.log, polls Worker   │  Python
   │               └──► Telegram (event-keyed, crash-safe)        │
   │          admin app (LAN-only, behind nginx)                  │
   │               └──► allowlist mutation ──► outbox ──► Worker  │
   └──────────────────────────────────────────────────────────────┘

   shared/   normalization-fixtures.json — the §4 contract BOTH halves
             must pass, in TypeScript and Python, from the same file
```

Authority boundaries (§1): Cloudflare accepts requests but cannot touch the
server. The host observes authenticated activity. Only a LAN-local process
alters the allowlist. Telegram is notification, never authority. The host
talks to the Worker API only — no D1 token exists on the host.

## Build order (§10) → what's where

| # | Step | Status |
|---|------|--------|
| 1 | §0 preconditions + capability probe | **Needs the live server.** Tooling ready: `whitelist-probe checklist / discover / add-test` |
| 2 | Log capture | **Needs the live server.** Workflow + validator: `host/captured/README.md`, `python -m whitelist_host.logparse check` |
| 3 | Normalization fixtures green in TS and Python + drift guard | Done — `shared/`, both test suites, daemon refuses to start on drift |
| 4 | Worker + form + D1 + email outbox | Done — `worker/` (status URL renders even with email down) |
| 5 | Matcher daemon | Done — polling, stored-attempt scan, eligibility window, conditional verify, event-keyed Telegram |
| 6 | Admin app | Done — review UI, tiered mutation with read-back, outbox with startup reconciliation + abandon |
| 7 | Cron | Done — nudge, both expiries, email retry ceiling, PII retention (incl. `notes`), attempt purge |

Steps 1–2 gate production trust: the shipped log patterns and the
Management-Protocol assumptions are defaults that MUST be validated against
the real server before the gateway is relied on. Bedrock especially — prefix
and space handling breaks silently, as a non-match.

## Deploying

**Cloudflare half** (`worker/README.md`): create the D1 database, apply
`worker/migrations/`, set the secrets (§9: Turnstile, Resend, daemon token,
admin token), deploy with wrangler. `whitelist.sambonius.net` is the only
proxied host — `mine.sambonius.net` stays DNS-only/grey-cloud (§0):
Minecraft is not HTTP, and Spectrum wouldn't cover Bedrock.

**Host half** (`host/README.md`): venv install, config +
systemd-credential secrets, two systemd units, nginx LAN-only front for the
admin app. Then run the §0 probe sequence before trusting any of it.

## Decisions the spec left open (recorded here)

- **Bedrock consoles: unsupported in v1**, stated plainly on the form before
  any fields (§5 required deciding before building the form). Flipping to a
  documented BedrockConnect workaround later is a copy change in
  `worker/src/html.ts`.
- **`notes` retention**: reviewer notes may contain PII, so retention nulls
  `real_name`, `email`, AND `notes` 90 days after `terminal_at` (§5's
  "pick one and state it").
- **Status link lives only in the HTTP response**: only the token's hash is
  stored, so emails cannot carry the link — receipt/nudge/decision emails
  are informational and tokenless by construction.
- **Verify replay after decision**: `/api/verify` keeps answering
  `{transitioned: false, attempt_event_id}` for the stored event id even
  once the row is approved/denied/expired, so a crashed daemon can always
  complete its Telegram notification (§6).

## Tests

```sh
cd worker && npm test        # node:sqlite D1 shim; no network
cd host && python -m pytest  # fakes for Worker/Telegram; real SQLite + files
```

Both suites load `shared/normalization-fixtures.json` — a normalization
change that lands in one language and not the other fails the other suite.

## Non-goals for v1 (§11)

Telegram inline approve buttons; multi-admin/per-admin auth; automatic
removal for inactivity; anything beyond Origin validation + custom header on
the LAN admin app.
