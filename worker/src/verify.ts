// POST /api/verify (daemon token) — §5.
//
// Contract:
//   - Validates that payload platform and normalized MATCH the submission,
//     not merely that the status is pending.
//   - Conditional atomic transition from 'pending' only. UUID, xuid,
//     raw_username, attempt_event_id, attempt_seen_at become immutable.
//   - The response always carries the STORED attempt_event_id:
//       transition won   → 200 {transitioned: true,  attempt_event_id}
//       identical replay → 200 {transitioned: false, attempt_event_id: <same>}
//       conflicting      → 409
//     The replay branch is what makes the daemon's crash-safe Telegram rule
//     (§6) work: a restarted daemon recognizes its own prior verification.
//   - Independently re-enforces the §3 rule that attempts older than the
//     60-minute lookback never auto-verify, even if a buggy daemon sends one.

import type { DBLike, Env } from './types.ts';
import { ATTEMPT_LOOKBACK_S } from './types.ts';
import { json } from './respond.ts';

interface VerifyPayload {
  submission_id: number;
  platform: string;
  normalized: string;
  raw_username: string;
  uuid: string;
  xuid?: string | null;
  attempt_event_id: string;
  attempt_seen_at: number;
}

function parsePayload(data: unknown): VerifyPayload | null {
  if (typeof data !== 'object' || data === null) return null;
  const d = data as Record<string, unknown>;
  if (
    !Number.isInteger(d.submission_id) ||
    (d.platform !== 'java' && d.platform !== 'bedrock') ||
    typeof d.normalized !== 'string' ||
    typeof d.raw_username !== 'string' ||
    typeof d.uuid !== 'string' ||
    d.uuid.length === 0 ||
    typeof d.attempt_event_id !== 'string' ||
    d.attempt_event_id.length === 0 ||
    !Number.isInteger(d.attempt_seen_at)
  ) {
    return null;
  }
  const xuid = d.xuid == null ? null : typeof d.xuid === 'string' ? d.xuid : undefined;
  if (xuid === undefined) return null;
  return {
    submission_id: d.submission_id as number,
    platform: d.platform,
    normalized: d.normalized,
    raw_username: d.raw_username,
    uuid: d.uuid,
    xuid,
    attempt_event_id: d.attempt_event_id,
    attempt_seen_at: d.attempt_seen_at as number,
  };
}

interface SubRow {
  id: number;
  platform: string;
  normalized: string;
  status: string;
  attempt_event_id: string | null;
  created_at: number;
}

export async function handleVerify(
  request: Request,
  env: Env,
  now: number,
): Promise<Response> {
  const db: DBLike = env.DB;
  let payload: VerifyPayload | null = null;
  try {
    payload = parsePayload(await request.json());
  } catch {
    /* fall through */
  }
  if (!payload) return json({ error: 'bad_payload' }, 400);

  const load = () =>
    db
      .prepare(
        `SELECT id, platform, normalized, status, attempt_event_id, created_at
         FROM submissions WHERE id = ?`,
      )
      .bind(payload.submission_id)
      .first<SubRow>();

  let row = await load();
  if (!row) return json({ error: 'not_found' }, 404);
  if (row.platform !== payload.platform || row.normalized !== payload.normalized) {
    return json({ error: 'identity_mismatch' }, 400);
  }
  if (payload.attempt_seen_at < row.created_at - ATTEMPT_LOOKBACK_S) {
    // §3: an attempt from before the lookback window proves the account was
    // once controlled, not that the submitter controls it now.
    return json({ error: 'attempt_too_old' }, 400);
  }

  if (row.status === 'pending') {
    const res = await db
      .prepare(
        `UPDATE submissions
         SET status = 'verified', uuid = ?, xuid = ?, raw_username = ?,
             attempt_event_id = ?, attempt_seen_at = ?, verified_at = ?
         WHERE id = ? AND status = 'pending'`,
      )
      .bind(
        payload.uuid,
        payload.xuid,
        payload.raw_username,
        payload.attempt_event_id,
        payload.attempt_seen_at,
        now,
        payload.submission_id,
      )
      .run();
    if (res.meta.changes === 1) {
      return json({ transitioned: true, attempt_event_id: payload.attempt_event_id });
    }
    row = await load(); // lost a race; fall through to replay/conflict logic
    if (!row) return json({ error: 'not_found' }, 404);
  }

  if (row.attempt_event_id !== null && row.attempt_event_id === payload.attempt_event_id) {
    return json({ transitioned: false, attempt_event_id: row.attempt_event_id });
  }
  return json(
    {
      error: 'conflict',
      status: row.status,
      attempt_event_id: row.attempt_event_id,
    },
    409,
  );
}
