// Email outbox (§5). One timestamp cannot represent receipt, nudge, and
// decision, so email_events carries one row per logical email with a stable
// idempotency key:
//
//   receipt/{submission_id}
//   nudge/{submission_id}
//   decision/{submission_id}/{decision}
//
// The key is passed to Resend as an Idempotency-Key AND tracked locally —
// Resend's keys expire after roughly 24 hours, shorter than our retry
// horizon, so local state is the real guard against duplicates.
//
// Deliberate property: no email ever contains the status token. Only the
// token's hash is stored, so the status URL cannot be reconstructed later —
// its canonical delivery is the HTTP response at submit time (§5).

import type { DBLike, Deps, Env } from './types.ts';
import { EMAIL_MAX_ATTEMPTS } from './types.ts';
import { escapeHtml } from './html.ts';

export function receiptKey(id: number): string {
  return `receipt/${id}`;
}
export function nudgeKey(id: number): string {
  return `nudge/${id}`;
}
export function decisionKey(id: number, decision: 'approved' | 'denied'): string {
  return `decision/${id}/${decision}`;
}

export async function queueEmail(
  db: DBLike,
  submissionId: number,
  kind: 'receipt' | 'nudge' | 'decision',
  key: string,
): Promise<void> {
  await db
    .prepare(
      `INSERT OR IGNORE INTO email_events (submission_id, kind, idempotency_key, state)
       VALUES (?, ?, ?, 'pending')`,
    )
    .bind(submissionId, kind, key)
    .run();
}

interface QueuedEmail {
  id: number;
  submission_id: number;
  kind: 'receipt' | 'nudge' | 'decision';
  idempotency_key: string;
  attempts_made: number;
  email: string | null;
  username: string;
  platform: string;
  status: string;
}

interface EmailContent {
  subject: string;
  text: string;
  html: string;
}

function para(lines: string[]): string {
  return lines.map((l) => `<p>${escapeHtml(l)}</p>`).join('\n');
}

export function buildEmail(
  env: Env,
  ev: Pick<QueuedEmail, 'kind' | 'idempotency_key' | 'username' | 'platform' | 'status'>,
): EmailContent | null {
  const addr = env.SERVER_ADDRESS;
  const who = `${ev.username} (${ev.platform === 'java' ? 'Java' : 'Bedrock'})`;
  if (ev.kind === 'receipt') {
    const lines = [
      `We received your whitelist request for ${who}.`,
      `Next step, if you have not done it yet: open Minecraft and try to connect to ${addr}. The connection WILL be refused — that refusal is a required part of the process, not an error. It is how we confirm the account is yours.`,
      `After we see that refused attempt, your request goes to a human for a decision. Your status page (the link shown right after you submitted) always has the current state.`,
    ];
    return {
      subject: `Whitelist request received — ${ev.username}`,
      text: lines.join('\n\n'),
      html: para(lines),
    };
  }
  if (ev.kind === 'nudge') {
    const lines = [
      `Your whitelist request for ${who} is still waiting for one thing: we have not yet seen a connection attempt from that account.`,
      `Open Minecraft and try to connect to ${addr}. The connection WILL be refused — that is expected and required. Without it, the request cannot be verified and will eventually expire.`,
    ];
    return {
      subject: `Action needed for your whitelist request — ${ev.username}`,
      text: lines.join('\n\n'),
      html: para(lines),
    };
  }
  // decision — the concrete decision rides in the key: decision/{id}/{decision}
  const decision = ev.idempotency_key.split('/')[2];
  if (decision === 'approved') {
    const lines = [
      `Good news: your whitelist request for ${who} was approved.`,
      `You can connect to ${addr} and play. See you in there!`,
    ];
    return {
      subject: `You're whitelisted — ${ev.username}`,
      text: lines.join('\n\n'),
      html: para(lines),
    };
  }
  if (decision === 'denied') {
    const lines = [
      `Your whitelist request for ${who} was not approved.`,
      `If you think this is a mistake, contact the server admin.`,
    ];
    return {
      subject: `Whitelist request update — ${ev.username}`,
      text: lines.join('\n\n'),
      html: para(lines),
    };
  }
  return null;
}

async function sendViaResend(
  fetchFn: typeof fetch,
  apiKey: string,
  from: string,
  to: string,
  content: EmailContent,
  idempotencyKey: string,
): Promise<{ ok: boolean; id?: string; error?: string }> {
  try {
    const res = await fetchFn('https://api.resend.com/emails', {
      method: 'POST',
      headers: {
        authorization: `Bearer ${apiKey}`,
        'content-type': 'application/json',
        'idempotency-key': idempotencyKey,
      },
      body: JSON.stringify({
        from,
        to: [to],
        subject: content.subject,
        text: content.text,
        html: content.html,
      }),
    });
    if (!res.ok) {
      const text = await res.text();
      return { ok: false, error: `resend ${res.status}: ${text.slice(0, 300)}` };
    }
    const data = (await res.json()) as { id?: string };
    return { ok: true, id: data.id };
  } catch (e) {
    return { ok: false, error: `fetch failed: ${String(e).slice(0, 300)}` };
  }
}

// Attempt delivery for due events. Runs from the daily cron and, best-effort,
// via ctx.waitUntil right after an event is queued. Retries stop at the
// attempt ceiling; a nudge whose submission is no longer pending is marked
// failed/superseded rather than sent late.
export async function processEmailQueue(
  db: DBLike,
  deps: Deps,
  env: Env,
  onlyKey?: string,
): Promise<void> {
  const filter = onlyKey ? ' AND e.idempotency_key = ?' : '';
  const stmt = db.prepare(
    `SELECT e.id, e.submission_id, e.kind, e.idempotency_key, e.attempts_made,
            s.email, s.username, s.platform, s.status
     FROM email_events e JOIN submissions s ON s.id = e.submission_id
     WHERE e.state IN ('pending', 'failed') AND e.attempts_made < ?${filter}
     ORDER BY e.id LIMIT 50`,
  );
  const bound = onlyKey
    ? stmt.bind(EMAIL_MAX_ATTEMPTS, onlyKey)
    : stmt.bind(EMAIL_MAX_ATTEMPTS);
  const { results } = await bound.all<QueuedEmail>();

  for (const ev of results) {
    const fail = (error: string) =>
      db
        .prepare(
          `UPDATE email_events SET state = 'failed', attempts_made = attempts_made + 1,
           last_error = ? WHERE id = ?`,
        )
        .bind(error, ev.id)
        .run();

    if (!ev.email) {
      await fail('no recipient (nulled by retention?)');
      continue;
    }
    if (ev.kind === 'nudge' && ev.status !== 'pending') {
      await fail('superseded: submission no longer pending');
      continue;
    }
    const content = buildEmail(env, ev);
    if (!content) {
      await fail(`unbuildable event key ${ev.idempotency_key}`);
      continue;
    }
    if (!env.RESEND_API_KEY) {
      await fail('RESEND_API_KEY not configured');
      continue;
    }
    const sent = await sendViaResend(
      deps.fetch,
      env.RESEND_API_KEY,
      env.EMAIL_FROM,
      ev.email,
      content,
      ev.idempotency_key,
    );
    if (sent.ok) {
      await db
        .prepare(
          `UPDATE email_events SET state = 'sent', attempts_made = attempts_made + 1,
           resend_id = ?, sent_at = ?, last_error = NULL WHERE id = ?`,
        )
        .bind(sent.id ?? null, deps.now(), ev.id)
        .run();
    } else {
      await fail(sent.error ?? 'unknown error');
    }
  }
}
