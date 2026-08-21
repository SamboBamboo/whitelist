// Bearer-token auth for the two API audiences (§9): the matcher daemon
// (read/verify) and the LAN admin app (review/decision). Split credentials —
// neither token grants the other side's operations.

import { tokenMatches } from './crypto.ts';

export type Audience = 'daemon' | 'admin';

export async function checkBearer(
  request: Request,
  expected: string | undefined,
): Promise<boolean> {
  if (!expected || expected.length === 0) return false; // unset secret: closed, never open
  const header = request.headers.get('authorization') ?? '';
  if (!header.startsWith('Bearer ')) return false;
  return tokenMatches(header.slice('Bearer '.length), expected);
}

export function unauthorized(): Response {
  return new Response(JSON.stringify({ error: 'unauthorized' }), {
    status: 401,
    headers: {
      'content-type': 'application/json',
      'cache-control': 'no-store',
      'www-authenticate': 'Bearer',
    },
  });
}
