// Fixed-window IP rate limit for the public form. Raw IPs are never stored;
// keys are SHA-256 of the connecting address.

import type { DBLike } from './types.ts';
import { RATE_LIMIT_MAX, RATE_LIMIT_WINDOW_S } from './types.ts';
import { sha256Hex } from './crypto.ts';

export async function allowSubmission(
  db: DBLike,
  ip: string | null,
  now: number,
): Promise<boolean> {
  const key = await sha256Hex(`ip:${ip ?? 'unknown'}`);
  const windowStart = Math.floor(now / RATE_LIMIT_WINDOW_S) * RATE_LIMIT_WINDOW_S;
  const row = await db
    .prepare(
      `INSERT INTO rate_limits (key, window_start, count) VALUES (?, ?, 1)
       ON CONFLICT(key, window_start) DO UPDATE SET count = count + 1
       RETURNING count`,
    )
    .bind(key, windowStart)
    .first<{ count: number }>();
  return (row?.count ?? 1) <= RATE_LIMIT_MAX;
}

export async function pruneRateLimits(db: DBLike, now: number): Promise<void> {
  await db
    .prepare('DELETE FROM rate_limits WHERE window_start < ?')
    .bind(now - 2 * RATE_LIMIT_WINDOW_S)
    .run();
}
