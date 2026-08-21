// Daily cron (§5): nudge, both expiries, email retry, PII retention.

import type { DBLike, Deps, Env } from './types.ts';
import { NUDGE_AFTER_S, PENDING_TTL_S, RETENTION_S, VERIFIED_TTL_S } from './types.ts';
import { processEmailQueue } from './email.ts';
import { pruneRateLimits } from './ratelimit.ts';

export async function runCron(env: Env, deps: Deps): Promise<void> {
  const db: DBLike = env.DB;
  const now = deps.now();

  // pending after 14 days → expired.
  await db
    .prepare(
      `UPDATE submissions SET status = 'expired', terminal_at = ?
       WHERE status = 'pending' AND created_at <= ?`,
    )
    .bind(now, now - PENDING_TTL_S)
    .run();

  // verified but undecided after 30 days → expired (otherwise verified rows
  // keep their PII forever).
  await db
    .prepare(
      `UPDATE submissions SET status = 'expired', terminal_at = ?
       WHERE status = 'verified' AND verified_at <= ?`,
    )
    .bind(now, now - VERIFIED_TTL_S)
    .run();

  // pending with no qualifying attempt after 48h → queue exactly one nudge.
  // (Still-pending at 48h IS "no qualifying attempt": a qualifying attempt
  // would have transitioned the row to verified.) The unique idempotency key
  // makes "one" structural.
  await db
    .prepare(
      `INSERT OR IGNORE INTO email_events (submission_id, kind, idempotency_key, state)
       SELECT id, 'nudge', 'nudge/' || id, 'pending'
       FROM submissions WHERE status = 'pending' AND created_at <= ?`,
    )
    .bind(now - NUDGE_AFTER_S)
    .run();

  // Backfill receipts for any recent submission whose receipt event was lost
  // between the two submit-time inserts (they are separate statements).
  await db
    .prepare(
      `INSERT OR IGNORE INTO email_events (submission_id, kind, idempotency_key, state)
       SELECT id, 'receipt', 'receipt/' || id, 'pending'
       FROM submissions WHERE created_at >= ?`,
    )
    .bind(now - 7 * 86400)
    .run();

  await processEmailQueue(db, deps, env);

  // Retention: 90 days after terminal, null real_name, email, AND notes.
  // Username and UUID are kept. (Stated policy: notes may contain PII typed
  // by the reviewer, so they are nulled here rather than policed in the UI.)
  await db
    .prepare(
      `UPDATE submissions SET real_name = NULL, email = NULL, notes = NULL
       WHERE terminal_at IS NOT NULL AND terminal_at <= ?
         AND (real_name IS NOT NULL OR email IS NOT NULL OR notes IS NOT NULL)`,
    )
    .bind(now - RETENTION_S)
    .run();

  await pruneRateLimits(db, now);
}
