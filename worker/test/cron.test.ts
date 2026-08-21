// Daily cron behaviors (§5): nudge, both expiries, email retry ceiling,
// PII retention including notes.

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { runCron } from '../src/cron.ts';
import { route } from '../src/index.ts';
import { apiRequest, makeWorld, submitRequest, T0 } from './helpers.ts';
import type { TestWorld } from './helpers.ts';

const DAY = 86400;

async function seed(world: TestWorld, username: string, platform = 'java', ip = '203.0.113.5') {
  const res = await route(
    submitRequest(
      {
        real_name: 'Jane Doe',
        email: 'jane@example.com',
        username,
        platform,
      },
      ip,
    ),
    world.env,
    world.deps,
  );
  assert.equal(res.status, 200);
  await world.flush(); // let the receipt send so later resend counts are clean
}

test('nudge queued exactly once for pending >48h, never for verified', async () => {
  const world = makeWorld();
  await seed(world, 'SlowPoke');
  await seed(world, 'FastOne', 'java', '198.51.100.3');
  // FastOne verifies immediately.
  const v = await route(
    apiRequest('/api/verify', {
      token: 'daemon-token',
      body: {
        submission_id: 2,
        platform: 'java',
        normalized: 'fastone',
        raw_username: 'FastOne',
        uuid: 'uuid-fast',
        xuid: null,
        attempt_event_id: 'ev-fast',
        attempt_seen_at: T0 + 60,
      },
    }),
    world.env,
    world.deps,
  );
  assert.equal(v.status, 200);

  world.clock.t = T0 + 3 * DAY;
  await runCron(world.env, world.deps);
  const nudges = world.db.rows("SELECT * FROM email_events WHERE kind = 'nudge'");
  assert.equal(nudges.length, 1);
  assert.equal(nudges[0].idempotency_key, 'nudge/1');
  assert.equal(nudges[0].state, 'sent');

  // Second cron run: still exactly one nudge, not re-sent.
  const callsBefore = world.net.resendCalls.length;
  await runCron(world.env, world.deps);
  assert.equal(world.db.rows("SELECT * FROM email_events WHERE kind = 'nudge'").length, 1);
  assert.equal(world.net.resendCalls.length, callsBefore);
});

test('pending expires at 14 days, verified-undecided at 30 days', async () => {
  const world = makeWorld();
  await seed(world, 'Pender');
  await seed(world, 'Verifier', 'java', '198.51.100.4');
  await route(
    apiRequest('/api/verify', {
      token: 'daemon-token',
      body: {
        submission_id: 2,
        platform: 'java',
        normalized: 'verifier',
        raw_username: 'Verifier',
        uuid: 'uuid-v',
        xuid: null,
        attempt_event_id: 'ev-v',
        attempt_seen_at: T0 + 60,
      },
    }),
    world.env,
    world.deps,
  );

  world.clock.t = T0 + 15 * DAY;
  await runCron(world.env, world.deps);
  assert.equal(world.db.row('SELECT status FROM submissions WHERE id = 1')?.status, 'expired');
  assert.equal(world.db.row('SELECT terminal_at FROM submissions WHERE id = 1')?.terminal_at, T0 + 15 * DAY);
  assert.equal(world.db.row('SELECT status FROM submissions WHERE id = 2')?.status, 'verified');

  world.clock.t = T0 + 31 * DAY;
  await runCron(world.env, world.deps);
  const v = world.db.row('SELECT status, terminal_at, uuid FROM submissions WHERE id = 2');
  assert.equal(v?.status, 'expired');
  assert.ok(v?.terminal_at);
  assert.equal(v?.uuid, 'uuid-v'); // uuid survives expiry

  // Expired slots free the unique index for a fresh submission.
  await seed(world, 'Pender', 'java', '198.51.100.6');
});

test('retention nulls real_name, email, AND notes 90 days after terminal', async () => {
  const world = makeWorld();
  await seed(world, 'OldTimer');
  const deny = await route(
    apiRequest('/api/decision', {
      token: 'admin-token',
      body: {
        submission_id: 1,
        decision: 'denied',
        reviewer: 'sam',
        notes: 'met them at work; seems fine but no',
      },
    }),
    world.env,
    world.deps,
  );
  assert.equal(deny.status, 200);
  await world.flush();

  world.clock.t = T0 + 89 * DAY;
  await runCron(world.env, world.deps);
  assert.equal(world.db.row('SELECT email FROM submissions WHERE id = 1')?.email, 'jane@example.com');

  world.clock.t = T0 + 91 * DAY;
  await runCron(world.env, world.deps);
  const row = world.db.row('SELECT * FROM submissions WHERE id = 1');
  assert.equal(row?.real_name, null);
  assert.equal(row?.email, null);
  assert.equal(row?.notes, null);
  assert.equal(row?.username, 'OldTimer'); // username and uuid are kept
});

test('email retry: failed sends retry until the attempt ceiling, then stop', async () => {
  const world = makeWorld();
  world.net.resendOk = false;
  await seed(world, 'Unlucky');
  const ev = () => world.db.row("SELECT * FROM email_events WHERE idempotency_key = 'receipt/1'");
  assert.equal(ev()?.state, 'failed');
  assert.equal(ev()?.attempts_made, 1);

  for (let day = 1; day <= 12; day++) {
    world.clock.t = T0 + day * 600; // keep well inside pending TTL
    await runCron(world.env, world.deps);
  }
  assert.equal(ev()?.attempts_made, 10); // EMAIL_MAX_ATTEMPTS
  assert.equal(ev()?.state, 'failed');
  const failedCalls = world.net.resendCalls.length;

  // Resend recovers, but the ceiling has been hit: no more attempts.
  world.net.resendOk = true;
  await runCron(world.env, world.deps);
  assert.equal(world.net.resendCalls.length, failedCalls);
});

test('nudge for a submission that verified before sending is superseded, not sent', async () => {
  const world = makeWorld();
  world.net.resendOk = false; // nothing sends yet
  await seed(world, 'Racer');
  world.clock.t = T0 + 3 * DAY;
  await runCron(world.env, world.deps); // queues nudge, send fails
  assert.equal(
    world.db.row("SELECT state FROM email_events WHERE kind='nudge'")?.state,
    'failed',
  );
  // Now the applicant verifies…
  await route(
    apiRequest('/api/verify', {
      token: 'daemon-token',
      body: {
        submission_id: 1,
        platform: 'java',
        normalized: 'racer',
        raw_username: 'Racer',
        uuid: 'uuid-r',
        xuid: null,
        attempt_event_id: 'ev-r',
        attempt_seen_at: T0 + 3 * DAY,
      },
    }),
    world.env,
    world.deps,
  );
  // …and email recovers. The stale nudge must not go out: no new attempt
  // beyond the failed pre-verification one.
  const nudgeAttemptsBefore = world.net.resendCalls.filter(
    (c) => c.idempotencyKey === 'nudge/1',
  ).length;
  world.net.resendOk = true;
  await runCron(world.env, world.deps);
  const nudge = world.db.row("SELECT * FROM email_events WHERE kind='nudge'");
  assert.equal(nudge?.state, 'failed');
  assert.match(String(nudge?.last_error), /superseded/);
  assert.equal(
    world.net.resendCalls.filter((c) => c.idempotencyKey === 'nudge/1').length,
    nudgeAttemptsBefore,
  );
});
