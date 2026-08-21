// POST /api/decision (admin token, idempotent) — §5.
//
// Rules:
//   - Approve ONLY from 'verified', and this backend independently confirms
//     a stored UUID exists. The admin app's disabled button is UX; this is
//     the rule.
//   - Deny from 'pending' or 'verified'.
//   - Identical repeat → success, no duplicate email (the outbox key
//     decision/{id}/{decision} is unique).
//   - Conflicting repeat → 409.
//   - UUID is immutable after verification; a decision carrying a different
//     one is rejected.
//   - Sets terminal_at. Queues a decision email event rather than sending
//     inline.

import type { DBLike, Deps, Env } from './types.ts';
import { json } from './respond.ts';
import { decisionKey, processEmailQueue, queueEmail } from './email.ts';

interface DecisionPayload {
  submission_id: number;
  decision: 'approved' | 'denied';
  uuid?: string | null;
  reviewer: string;
  notes?: string | null;
}

function parsePayload(data: unknown): DecisionPayload | null {
  if (typeof data !== 'object' || data === null) return null;
  const d = data as Record<string, unknown>;
  if (
    !Number.isInteger(d.submission_id) ||
    (d.decision !== 'approved' && d.decision !== 'denied') ||
    typeof d.reviewer !== 'string' ||
    d.reviewer.length === 0 ||
    d.reviewer.length > 80
  ) {
    return null;
  }
  const uuid = d.uuid == null ? null : typeof d.uuid === 'string' ? d.uuid : undefined;
  const notes = d.notes == null ? null : typeof d.notes === 'string' ? d.notes : undefined;
  if (uuid === undefined || notes === undefined) return null;
  if (notes && notes.length > 2000) return null;
  return {
    submission_id: d.submission_id as number,
    decision: d.decision,
    uuid,
    reviewer: d.reviewer,
    notes,
  };
}

interface SubRow {
  id: number;
  status: string;
  uuid: string | null;
}

export async function handleDecision(
  request: Request,
  env: Env,
  deps: Deps,
): Promise<Response> {
  const db: DBLike = env.DB;
  let payload: DecisionPayload | null = null;
  try {
    payload = parsePayload(await request.json());
  } catch {
    /* fall through */
  }
  if (!payload) return json({ error: 'bad_payload' }, 400);

  const load = () =>
    db
      .prepare('SELECT id, status, uuid FROM submissions WHERE id = ?')
      .bind(payload.submission_id)
      .first<SubRow>();

  let row = await load();
  if (!row) return json({ error: 'not_found' }, 404);

  // UUID immutability: any decision naming a different UUID than the stored
  // one is rejected outright, whatever the states involved.
  if (payload.uuid != null && row.uuid != null && payload.uuid !== row.uuid) {
    return json({ error: 'uuid_mismatch', status: row.status }, 409);
  }

  const finish = async (transitioned: boolean) => {
    const key = decisionKey(payload.submission_id, payload.decision);
    await queueEmail(db, payload.submission_id, 'decision', key);
    if (transitioned) deps.defer(processEmailQueue(db, deps, env, key));
    return json({ ok: true, status: payload.decision, transitioned });
  };

  for (let attempt = 0; attempt < 2; attempt++) {
    // Identical repeat → success without a second transition.
    if (row.status === payload.decision) return finish(false);

    if (payload.decision === 'approved') {
      if (row.status !== 'verified') {
        return json({ error: 'invalid_state', status: row.status }, 409);
      }
      if (row.uuid == null) {
        // Independent backend confirmation that verification stored a UUID.
        return json({ error: 'no_uuid', status: row.status }, 409);
      }
      if (payload.uuid == null || payload.uuid !== row.uuid) {
        return json({ error: 'uuid_mismatch', status: row.status }, 409);
      }
      const now = deps.now();
      const res = await db
        .prepare(
          `UPDATE submissions
           SET status = 'approved', reviewer = ?, notes = ?, decided_at = ?, terminal_at = ?
           WHERE id = ? AND status = 'verified' AND uuid = ?`,
        )
        .bind(payload.reviewer, payload.notes, now, now, row.id, payload.uuid)
        .run();
      if (res.meta.changes === 1) return finish(true);
    } else {
      if (row.status !== 'pending' && row.status !== 'verified') {
        return json({ error: 'invalid_state', status: row.status }, 409);
      }
      const now = deps.now();
      const res = await db
        .prepare(
          `UPDATE submissions
           SET status = 'denied', reviewer = ?, notes = ?, decided_at = ?, terminal_at = ?
           WHERE id = ? AND status IN ('pending', 'verified')`,
        )
        .bind(payload.reviewer, payload.notes, now, now, row.id)
        .run();
      if (res.meta.changes === 1) return finish(true);
    }

    // Lost a race; reload once and re-evaluate.
    row = await load();
    if (!row) return json({ error: 'not_found' }, 404);
  }
  return json({ error: 'conflict', status: row.status }, 409);
}
