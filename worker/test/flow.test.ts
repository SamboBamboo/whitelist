// End-to-end tests of the Worker request flow (§5): submit → status →
// pending → verify → decision, including the replay and conflict contracts.

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { route } from '../src/index.ts';
import { apiRequest, makeWorld, submitRequest, T0 } from './helpers.ts';
import type { TestWorld } from './helpers.ts';

const JANE = {
  real_name: 'Jane Doe',
  email: 'jane@example.com',
  username: 'Cave Johnson',
  platform: 'bedrock',
};

async function submit(world: TestWorld, fields: Record<string, string>, ip?: string) {
  const res = await route(submitRequest(fields, ip), world.env, world.deps);
  return { res, html: await res.text() };
}

function extractToken(html: string): string {
  const m = html.match(/\/status\/([A-Za-z0-9_-]+)/);
  assert.ok(m, 'status URL present in response');
  return m[1];
}

const VERIFY_BODY = {
  submission_id: 1,
  platform: 'bedrock',
  normalized: 'cave_johnson',
  raw_username: '.Cave_Johnson',
  uuid: '00000000-0000-0000-0009-01f64f6dd58e',
  xuid: '2535405290989773',
  attempt_event_id: 'ev-abc123',
  attempt_seen_at: T0 + 120,
};

test('submit renders status URL immediately and independent of email', async () => {
  const world = makeWorld();
  world.net.resendOk = false; // Resend down — the page must still work
  const { res, html } = await submit(world, JANE);
  assert.equal(res.status, 200);
  const token = extractToken(html);
  await world.flush();

  const row = world.db.row('SELECT * FROM submissions WHERE id = 1');
  assert.ok(row);
  assert.equal(row.status, 'pending');
  assert.equal(row.username, 'Cave Johnson');
  assert.equal(row.normalized, 'cave_johnson'); // forward Floodgate transform
  assert.equal(row.platform, 'bedrock');
  assert.equal(row.normalization_version, 1);
  assert.equal(row.real_name, 'Jane Doe');

  // Receipt queued but failed — and the applicant still has a working link.
  const ev = world.db.row(
    "SELECT * FROM email_events WHERE idempotency_key = 'receipt/1'",
  );
  assert.ok(ev);
  assert.equal(ev.state, 'failed');

  const statusRes = await route(
    apiRequest(`/status/${token}`),
    world.env,
    world.deps,
  );
  assert.equal(statusRes.status, 200);
  assert.equal(statusRes.headers.get('cache-control'), 'no-store');
  const statusHtml = await statusRes.text();
  assert.match(statusHtml, /Cave Johnson/);
  assert.match(statusHtml, /refused|Waiting for your/i);
  assert.doesNotMatch(statusHtml, /Jane Doe/); // no PII beyond the username
  assert.doesNotMatch(statusHtml, /jane@example\.com/);
});

test('receipt email sends when Resend is up, with the stable idempotency key', async () => {
  const world = makeWorld();
  await submit(world, JANE);
  await world.flush();
  assert.equal(world.net.resendCalls.length, 1);
  assert.equal(world.net.resendCalls[0].idempotencyKey, 'receipt/1');
  const ev = world.db.row("SELECT * FROM email_events WHERE idempotency_key = 'receipt/1'");
  assert.equal(ev?.state, 'sent');
  // The email must not contain a status link — only its hash is stored.
  const body = world.net.resendCalls[0].body as { text: string };
  assert.doesNotMatch(body.text, /\/status\//);
});

test('duplicate active submission is rejected by the partial unique index', async () => {
  const world = makeWorld();
  await submit(world, JANE);
  // Different claimed spelling, same normalized key → still a duplicate.
  const { res } = await submit(world, { ...JANE, username: 'cave_johnson' }, '203.0.113.99');
  assert.equal(res.status, 409);
  // Same name on the other platform is fine.
  const { res: res2 } = await submit(
    world,
    { ...JANE, username: 'cave_johnson', platform: 'java' },
    '203.0.113.99',
  );
  assert.equal(res2.status, 200);
});

test('turnstile failure blocks, rate limit trips on the sixth submission', async () => {
  const world = makeWorld();
  world.net.turnstileOk = false;
  const { res } = await submit(world, JANE);
  assert.equal(res.status, 400);
  assert.equal(world.db.rows('SELECT * FROM submissions').length, 0);

  world.net.turnstileOk = true;
  for (let i = 0; i < 5; i++) {
    const { res: r } = await submit(world, { ...JANE, username: `Player${i}`, platform: 'java' });
    assert.equal(r.status, 200, `submission ${i}`);
  }
  const { res: sixth } = await submit(world, { ...JANE, username: 'PlayerSix', platform: 'java' });
  assert.equal(sixth.status, 429);
  // A different IP is not affected.
  const { res: other } = await submit(
    world,
    { ...JANE, username: 'PlayerSeven', platform: 'java' },
    '198.51.100.7',
  );
  assert.equal(other.status, 200);
});

test('java validation rejects bad names, bedrock accepts spaces', async () => {
  const world = makeWorld();
  const { res } = await submit(world, {
    ...JANE,
    username: 'Cave Johnson',
    platform: 'java',
  });
  assert.equal(res.status, 400); // spaces are not legal in Java names
  const { res: res2 } = await submit(world, { ...JANE, username: 'ab', platform: 'java' });
  assert.equal(res2.status, 400); // too short
});

test('pending endpoint requires the daemon token and serves match keys + drift config', async () => {
  const world = makeWorld();
  await submit(world, JANE);

  const noAuth = await route(apiRequest('/api/pending'), world.env, world.deps);
  assert.equal(noAuth.status, 401);
  const wrongToken = await route(
    apiRequest('/api/pending', { token: 'admin-token' }),
    world.env,
    world.deps,
  );
  assert.equal(wrongToken.status, 401);

  const res = await route(
    apiRequest('/api/pending', { token: 'daemon-token' }),
    world.env,
    world.deps,
  );
  assert.equal(res.status, 200);
  assert.equal(res.headers.get('cache-control'), 'no-store');
  const data = (await res.json()) as {
    normalization: { username_prefix: string; replace_spaces: boolean; version: number };
    pending: { id: number; normalized: string; expires_at: number; created_at: number }[];
  };
  assert.deepEqual(data.normalization, {
    username_prefix: '.',
    replace_spaces: true,
    version: 1,
  });
  assert.equal(data.pending.length, 1);
  assert.equal(data.pending[0].normalized, 'cave_johnson');
  assert.equal(data.pending[0].expires_at, data.pending[0].created_at + 14 * 86400);
});

async function verify(world: TestWorld, body: unknown) {
  const res = await route(
    apiRequest('/api/verify', { token: 'daemon-token', body }),
    world.env,
    world.deps,
  );
  return { res, data: (await res.json()) as Record<string, unknown> };
}

test('verify: transition, identical replay, conflicting attempt', async () => {
  const world = makeWorld();
  await submit(world, JANE);

  // Identity mismatch is rejected before any state check.
  const wrongIdentity = await verify(world, { ...VERIFY_BODY, normalized: 'someone_else' });
  assert.equal(wrongIdentity.res.status, 400);
  assert.equal(wrongIdentity.data.error, 'identity_mismatch');

  // The 60-minute lookback is re-enforced server-side: older attempts never verify.
  const tooOld = await verify(world, { ...VERIFY_BODY, attempt_seen_at: T0 - 3601 });
  assert.equal(tooOld.res.status, 400);
  assert.equal(tooOld.data.error, 'attempt_too_old');
  // ...but an attempt from 59 minutes before submission is fine (attempt-first path).
  const world2 = makeWorld();
  await submit(world2, JANE);
  const early = await verify(world2, { ...VERIFY_BODY, attempt_seen_at: T0 - 3540 });
  assert.equal(early.res.status, 200);

  // Transition wins exactly once.
  const first = await verify(world, VERIFY_BODY);
  assert.equal(first.res.status, 200);
  assert.deepEqual(first.data, { transitioned: true, attempt_event_id: 'ev-abc123' });
  const row = world.db.row('SELECT * FROM submissions WHERE id = 1');
  assert.equal(row?.status, 'verified');
  assert.equal(row?.uuid, VERIFY_BODY.uuid);
  assert.equal(row?.raw_username, '.Cave_Johnson');
  assert.equal(row?.xuid, '2535405290989773');
  assert.equal(typeof row?.xuid, 'string'); // TEXT, never a number

  // Identical replay: 200, transitioned false, SAME stored event id.
  const replay = await verify(world, VERIFY_BODY);
  assert.equal(replay.res.status, 200);
  assert.deepEqual(replay.data, { transitioned: false, attempt_event_id: 'ev-abc123' });

  // A different attempt against the now-verified row: 409, stored id returned.
  const conflict = await verify(world, { ...VERIFY_BODY, attempt_event_id: 'ev-other' });
  assert.equal(conflict.res.status, 409);
  assert.equal(conflict.data.attempt_event_id, 'ev-abc123');
});

async function decide(world: TestWorld, body: unknown) {
  const res = await route(
    apiRequest('/api/decision', { token: 'admin-token', body }),
    world.env,
    world.deps,
  );
  return { res, data: (await res.json()) as Record<string, unknown> };
}

test('decision: approve only from verified with matching stored uuid', async () => {
  const world = makeWorld();
  await submit(world, JANE);

  const approve = {
    submission_id: 1,
    decision: 'approved',
    uuid: VERIFY_BODY.uuid,
    reviewer: 'sam',
    notes: '',
  };

  // Approving a merely-pending submission is refused — the disabled button
  // in the admin UI is UX; this is the rule.
  const early = await decide(world, approve);
  assert.equal(early.res.status, 409);
  assert.equal(early.data.error, 'invalid_state');

  await verify(world, VERIFY_BODY);

  // Wrong UUID is refused: immutable after verification.
  const wrongUuid = await decide(world, { ...approve, uuid: 'different-uuid' });
  assert.equal(wrongUuid.res.status, 409);
  assert.equal(wrongUuid.data.error, 'uuid_mismatch');

  const ok = await decide(world, approve);
  assert.equal(ok.res.status, 200);
  assert.deepEqual(ok.data, { ok: true, status: 'approved', transitioned: true });
  const row = world.db.row('SELECT * FROM submissions WHERE id = 1');
  assert.equal(row?.status, 'approved');
  assert.equal(row?.reviewer, 'sam');
  assert.ok(row?.terminal_at);
  await world.flush();
  const decisionCalls = world.net.resendCalls.filter(
    (c) => c.idempotencyKey === 'decision/1/approved',
  );
  assert.equal(decisionCalls.length, 1);

  // Identical repeat: success, no duplicate email.
  const repeat = await decide(world, approve);
  assert.equal(repeat.res.status, 200);
  assert.deepEqual(repeat.data, { ok: true, status: 'approved', transitioned: false });
  await world.flush();
  assert.equal(
    world.net.resendCalls.filter((c) => c.idempotencyKey === 'decision/1/approved').length,
    1,
  );
  assert.equal(
    world.db.rows("SELECT * FROM email_events WHERE kind = 'decision'").length,
    1,
  );

  // Conflicting repeat: 409.
  const flip = await decide(world, { ...approve, decision: 'denied' });
  assert.equal(flip.res.status, 409);

  // Verify replay after approval still confirms the original event id, so a
  // crashed daemon can complete its Telegram notification.
  const lateReplay = await verify(world, VERIFY_BODY);
  assert.equal(lateReplay.res.status, 200);
  assert.deepEqual(lateReplay.data, { transitioned: false, attempt_event_id: 'ev-abc123' });
});

test('decision: deny works from pending and from verified', async () => {
  const world = makeWorld();
  await submit(world, JANE);
  const deny = { submission_id: 1, decision: 'denied', reviewer: 'sam' };
  const ok = await decide(world, deny);
  assert.equal(ok.res.status, 200);
  assert.equal(world.db.row('SELECT status FROM submissions WHERE id = 1')?.status, 'denied');
  const repeat = await decide(world, deny);
  assert.deepEqual(repeat.data, { ok: true, status: 'denied', transitioned: false });

  // Denied is terminal: the slot frees up for a fresh submission.
  const { res } = await submit(world, JANE, '198.51.100.9');
  assert.equal(res.status, 200);
});

test('review endpoint serves pending and verified rows with attempt details', async () => {
  const world = makeWorld();
  await submit(world, JANE);
  await submit(world, { ...JANE, username: 'Foo_Bar', platform: 'java' }, '198.51.100.2');
  await verify(world, VERIFY_BODY);

  const unauth = await route(apiRequest('/api/review', { token: 'daemon-token' }), world.env, world.deps);
  assert.equal(unauth.status, 401); // daemon token cannot read review

  const res = await route(apiRequest('/api/review', { token: 'admin-token' }), world.env, world.deps);
  assert.equal(res.status, 200);
  const data = (await res.json()) as {
    submissions: Record<string, unknown>[];
  };
  assert.equal(data.submissions.length, 2);
  const verified = data.submissions.find((s) => s.status === 'verified');
  assert.ok(verified);
  assert.equal(verified.raw_username, '.Cave_Johnson');
  assert.equal(verified.attempt_event_id, 'ev-abc123');
  assert.equal(verified.real_name, 'Jane Doe');
});
