// GET /api/pending (daemon token) — §5.
//
// Serves three things to the matcher daemon in one authenticated response:
//   1. `normalization`: the Worker's pinned normalization config, so the
//      daemon can run the §4 config-drift guard on every poll.
//   2. `pending`: submissions the daemon should try to match, with the match
//      keys, created_at, per-row normalization_version, and the explicit
//      eligibility upper bound (expires_at) so policy lives here, not there.
//   3. `recently_verified`: enough submission detail for the daemon to
//      compose its Telegram message even after a crash — /api/verify replays
//      return `transitioned: false`, and by then the row is no longer in
//      `pending`, so the crash-safe notification rule (§6) needs this.

import type { DBLike, Env } from './types.ts';
import { PENDING_TTL_S, normConfig } from './types.ts';
import { json } from './respond.ts';

export async function handlePending(env: Env, now: number): Promise<Response> {
  const db: DBLike = env.DB;
  const cfg = normConfig(env);

  const pending = await db
    .prepare(
      `SELECT id, username, real_name, platform, normalized, normalization_version, created_at
       FROM submissions WHERE status = 'pending' ORDER BY created_at`,
    )
    .all();

  const recentlyVerified = await db
    .prepare(
      `SELECT id, username, real_name, platform, normalized, uuid, xuid,
              attempt_event_id, verified_at, status
       FROM submissions
       WHERE verified_at IS NOT NULL AND (status = 'verified' OR verified_at >= ?)
       ORDER BY verified_at DESC LIMIT 100`,
    )
    .bind(now - 7 * 86400)
    .all();

  return json({
    now,
    normalization: {
      username_prefix: cfg.prefix,
      replace_spaces: cfg.replaceSpaces,
      version: cfg.version,
    },
    pending: pending.results.map((r) => ({
      ...r,
      expires_at: (r.created_at as number) + PENDING_TTL_S,
    })),
    recently_verified: recentlyVerified.results,
  });
}
