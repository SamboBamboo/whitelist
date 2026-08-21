# Cloudflare half — Worker + D1

`whitelist.sambonius.net`: server-rendered public form, the four
authenticated API endpoints, the email outbox, and the daily cron (§5).

## Layout

```
src/
  index.ts     router + auth boundaries + scheduled handler
  form.ts      GET / and POST /api/submit (Turnstile, rate limit, dup index)
  status.ts    GET /status/:token (hash lookup, no PII, no-store)
  pending.ts   GET /api/pending   (daemon token; drift config + telegram data)
  review.ts    GET /api/review    (admin token; pending+verified+recent)
  verify.ts    POST /api/verify   (atomic transition; replay contract)
  decision.ts  POST /api/decision (idempotent; approve only from verified)
  email.ts     outbox with stable keys receipt/ nudge/ decision/
  cron.ts      expiries, nudges, retries, PII retention
  normalize.ts §4 forward-only normalization (shared fixtures)
migrations/    D1 schema (§8) — partial unique index enforces one active
               submission per (platform, normalized)
```

## Deploy

```sh
cd worker
npm install
npx wrangler d1 create whitelist          # put the id in wrangler.toml
npm run migrate                           # applies migrations/ remotely
npx wrangler secret put TURNSTILE_SECRET
npx wrangler secret put RESEND_API_KEY
npx wrangler secret put DAEMON_TOKEN      # generate: openssl rand -base64 32
npx wrangler secret put ADMIN_API_TOKEN   # a DIFFERENT random value
npm run deploy
```

Also set in `wrangler.toml` before deploying: the Turnstile **site** key,
the D1 database id, and — only if they differ from the defaults — the
normalization pins (`FLOODGATE_PREFIX`, `REPLACE_SPACES`,
`NORMALIZATION_VERSION`), which must match the host's real Floodgate config
or the daemon will refuse to run (§4). `EMAIL_FROM` needs a Resend-verified
sending domain. `BEDROCK_PORT` should be confirmed against the actual
Geyser bind.

For local development: `npm run migrate:local && npm run dev`, with
Turnstile's always-pass test keys
(site `1x00000000000000000000AA`, secret `1x0000000000000000000000000000000AA`).

## Tests

```sh
npm test         # node --test via type stripping; D1 shimmed onto node:sqlite
npm run typecheck
```

The shim runs the real SQL against real SQLite, so the partial unique
index, conditional transitions, replay semantics, and cron updates are
exercised as they will run on D1.
