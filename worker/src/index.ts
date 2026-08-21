// Worker entry point: routing, auth boundaries, and the scheduled handler.
//
// Authority boundaries (§1): this Worker accepts requests but cannot touch
// the Minecraft server. The daemon token can read pending and post verify;
// the admin token can read review and post decisions. Neither can do the
// other's job.

import type { Deps, Env } from './types.ts';
import { checkBearer, unauthorized } from './auth.ts';
import { formPage, handleSubmit } from './form.ts';
import { handleStatus } from './status.ts';
import { handlePending } from './pending.ts';
import { handleReview } from './review.ts';
import { handleVerify } from './verify.ts';
import { handleDecision } from './decision.ts';
import { runCron } from './cron.ts';
import { json } from './respond.ts';

interface ExecutionCtx {
  waitUntil(p: Promise<unknown>): void;
}

function makeDeps(ctx: ExecutionCtx): Deps {
  return {
    now: () => Math.floor(Date.now() / 1000),
    fetch: (input: RequestInfo | URL, init?: RequestInit) => fetch(input, init),
    defer: (p) => ctx.waitUntil(p.catch(() => {})),
  };
}

export async function route(
  request: Request,
  env: Env,
  deps: Deps,
): Promise<Response> {
  const url = new URL(request.url);
  const path = url.pathname;
  const method = request.method;

  if (path === '/' && method === 'GET') return formPage(env);
  if (path === '/api/submit' && method === 'POST') return handleSubmit(request, env, deps);

  const statusMatch = path.match(/^\/status\/([A-Za-z0-9_-]+)$/);
  if (statusMatch && method === 'GET') return handleStatus(statusMatch[1], env);

  // Daemon-token surface.
  if (path === '/api/pending' || path === '/api/verify') {
    if (!(await checkBearer(request, env.DAEMON_TOKEN))) return unauthorized();
    if (path === '/api/pending' && method === 'GET') return handlePending(env, deps.now());
    if (path === '/api/verify' && method === 'POST')
      return handleVerify(request, env, deps.now());
    return json({ error: 'method_not_allowed' }, 405);
  }

  // Admin-token surface.
  if (path === '/api/review' || path === '/api/decision') {
    if (!(await checkBearer(request, env.ADMIN_API_TOKEN))) return unauthorized();
    if (path === '/api/review' && method === 'GET') return handleReview(env, deps.now());
    if (path === '/api/decision' && method === 'POST')
      return handleDecision(request, env, deps);
    return json({ error: 'method_not_allowed' }, 405);
  }

  return new Response('Not found', { status: 404 });
}

export default {
  async fetch(request: Request, env: Env, ctx: ExecutionCtx): Promise<Response> {
    try {
      return await route(request, env, makeDeps(ctx));
    } catch (e) {
      console.error('unhandled error', e);
      return json({ error: 'internal' }, 500);
    }
  },

  async scheduled(_event: unknown, env: Env, ctx: ExecutionCtx): Promise<void> {
    await runCron(env, makeDeps(ctx));
  },
};
