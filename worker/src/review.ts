// GET /api/review (admin token) — §5. Pending AND verified rows with attempt
// details for the admin app, plus a short tail of recently decided rows for
// context. (v2's gap — the admin had no defined way to fetch verified rows —
// is closed here.)

import type { DBLike, Env } from './types.ts';
import { PENDING_TTL_S, VERIFIED_TTL_S, normConfig } from './types.ts';
import { json } from './respond.ts';

export async function handleReview(env: Env, now: number): Promise<Response> {
  const db: DBLike = env.DB;
  const cfg = normConfig(env);

  const open = await db
    .prepare(
      `SELECT id, real_name, email, notes, username, raw_username, normalized,
              normalization_version, platform, status, uuid, xuid,
              attempt_event_id, attempt_seen_at, created_at, verified_at
       FROM submissions WHERE status IN ('pending', 'verified')
       ORDER BY created_at`,
    )
    .all();

  const recentTerminal = await db
    .prepare(
      `SELECT id, username, platform, status, uuid, reviewer, decided_at, terminal_at
       FROM submissions WHERE terminal_at IS NOT NULL
       ORDER BY terminal_at DESC LIMIT 25`,
    )
    .all();

  return json({
    now,
    normalization: {
      username_prefix: cfg.prefix,
      replace_spaces: cfg.replaceSpaces,
      version: cfg.version,
    },
    submissions: open.results.map((r) => ({
      ...r,
      expires_at:
        r.status === 'pending'
          ? (r.created_at as number) + PENDING_TTL_S
          : (r.verified_at as number) + VERIFIED_TTL_S,
    })),
    recent_terminal: recentTerminal.results,
  });
}
