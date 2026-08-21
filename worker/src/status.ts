// GET /status/:token — applicant-facing status page. Status only, no PII
// beyond the username (§5). Always Cache-Control: no-store.

import type { DBLike, Env } from './types.ts';
import { sha256Hex } from './crypto.ts';
import { connectInstructions, escapeHtml, htmlResponse, page } from './html.ts';

interface StatusRow {
  username: string;
  platform: string;
  status: string;
  created_at: number;
  verified_at: number | null;
  decided_at: number | null;
}

function dot(state: 'done' | 'todo' | 'bad', label: string): string {
  return `<div class="statusline"><span class="dot ${state}"></span><span>${label}</span></div>`;
}

function renderStatus(env: Env, row: StatusRow): string {
  const name = escapeHtml(row.username);
  const platform = row.platform === 'java' ? 'Java' : 'Bedrock';
  let timeline = dot('done', `Request submitted for <strong>${name}</strong> (${platform}).`);
  let tail = '';
  switch (row.status) {
    case 'pending':
      timeline += dot('todo', 'Waiting for your refused connection attempt.');
      timeline += dot('todo', 'Waiting for a human decision.');
      tail = connectInstructions(
        { serverAddress: env.SERVER_ADDRESS, bedrockPort: env.BEDROCK_PORT },
        row.platform,
      );
      break;
    case 'verified':
      timeline += dot('done', 'Connection attempt seen — account verified.');
      timeline += dot('todo', 'Waiting for a human decision.');
      tail = '<p>Nothing more to do. A decision usually follows within a few days.</p>';
      break;
    case 'approved':
      timeline += dot('done', 'Connection attempt seen — account verified.');
      timeline += dot(
        'done',
        `<strong>Approved.</strong> Connect to <span class="mono addr">${escapeHtml(env.SERVER_ADDRESS)}</span> and play.`,
      );
      break;
    case 'denied':
      timeline += dot('bad', 'The request was not approved.');
      tail = '<p>If you think this is a mistake, contact the server admin.</p>';
      break;
    case 'expired':
      timeline += dot('bad', 'The request expired before it was completed.');
      tail = '<p><a href="/">Submit a new request</a> if you still want in.</p>';
      break;
  }
  return page(
    `Whitelist status — ${row.username}`,
    `<h1>Whitelist request status</h1><div class="card">${timeline}</div>${tail}`,
  );
}

export async function handleStatus(
  token: string,
  env: Env,
): Promise<Response> {
  const db: DBLike = env.DB;
  const noStore = { 'cache-control': 'no-store' };
  if (!/^[A-Za-z0-9_-]{20,64}$/.test(token)) {
    return htmlResponse(notFoundPage(), 404, noStore);
  }
  const hash = await sha256Hex(token);
  const row = await db
    .prepare(
      `SELECT username, platform, status, created_at, verified_at, decided_at
       FROM submissions WHERE token_hash = ?`,
    )
    .bind(hash)
    .first<StatusRow>();
  if (!row) return htmlResponse(notFoundPage(), 404, noStore);
  return htmlResponse(renderStatus(env, row), 200, noStore);
}

function notFoundPage(): string {
  return page(
    'Unknown status link',
    `<h1>Unknown status link</h1>
<div class="card"><p>This status link doesn't match any request. Links are only shown
once, right after submitting. If you lost yours, decisions still arrive by email.</p>
<p><a href="/">Back to the form</a></p></div>`,
  );
}
