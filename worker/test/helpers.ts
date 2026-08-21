// Test scaffolding: a node:sqlite shim for the D1 subset the Worker uses,
// plus Env/Deps builders with a controllable clock and a stub network.
// Running the real SQL against real SQLite matters here — the partial unique
// index and conditional updates ARE the logic under test.

import { DatabaseSync } from 'node:sqlite';
import { readFileSync } from 'node:fs';
import type { DBLike, DBStatement, Deps, Env } from '../src/types.ts';

const MIGRATION = readFileSync(
  new URL('../migrations/0001_init.sql', import.meta.url),
  'utf8',
);

class ShimStatement implements DBStatement {
  #db: DatabaseSync;
  #sql: string;
  #args: unknown[] = [];

  constructor(db: DatabaseSync, sql: string) {
    this.#db = db;
    this.#sql = sql;
  }

  bind(...values: unknown[]): DBStatement {
    this.#args = values;
    return this;
  }

  async first<T>(): Promise<T | null> {
    const stmt = this.#db.prepare(this.#sql);
    const row = stmt.get(...(this.#args as never[]));
    return (row as T | undefined) ?? null;
  }

  async all<T>(): Promise<{ results: T[] }> {
    const stmt = this.#db.prepare(this.#sql);
    return { results: stmt.all(...(this.#args as never[])) as T[] };
  }

  async run(): Promise<{ meta: { changes: number; last_row_id: number } }> {
    const stmt = this.#db.prepare(this.#sql);
    const res = stmt.run(...(this.#args as never[]));
    return {
      meta: { changes: Number(res.changes), last_row_id: Number(res.lastInsertRowid) },
    };
  }
}

export class ShimDB implements DBLike {
  raw: DatabaseSync;

  constructor() {
    this.raw = new DatabaseSync(':memory:');
    this.raw.exec(MIGRATION);
  }

  prepare(sql: string): DBStatement {
    return new ShimStatement(this.raw, sql);
  }

  row(sql: string, ...args: unknown[]): Record<string, unknown> | undefined {
    return this.raw.prepare(sql).get(...(args as never[])) as
      | Record<string, unknown>
      | undefined;
  }

  rows(sql: string, ...args: unknown[]): Record<string, unknown>[] {
    return this.raw.prepare(sql).all(...(args as never[])) as Record<string, unknown>[];
  }
}

export interface StubNet {
  turnstileOk: boolean;
  resendOk: boolean;
  resendCalls: { url: string; idempotencyKey: string | null; body: unknown }[];
  turnstileCalls: number;
}

export interface TestWorld {
  db: ShimDB;
  env: Env;
  deps: Deps;
  net: StubNet;
  clock: { t: number };
  flush(): Promise<void>; // await everything passed to defer()
}

export const T0 = 1_750_000_000; // fixed test epoch

export function makeWorld(): TestWorld {
  const db = new ShimDB();
  const clock = { t: T0 };
  const net: StubNet = { turnstileOk: true, resendOk: true, resendCalls: [], turnstileCalls: 0 };
  const deferred: Promise<unknown>[] = [];

  const stubFetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input instanceof Request ? input.url : input);
    if (url.includes('challenges.cloudflare.com')) {
      net.turnstileCalls++;
      return new Response(JSON.stringify({ success: net.turnstileOk }), {
        status: 200,
        headers: { 'content-type': 'application/json' },
      });
    }
    if (url.includes('api.resend.com')) {
      const headers = new Headers(init?.headers);
      net.resendCalls.push({
        url,
        idempotencyKey: headers.get('idempotency-key'),
        body: init?.body ? JSON.parse(String(init.body)) : null,
      });
      if (!net.resendOk) return new Response('boom', { status: 500 });
      return new Response(JSON.stringify({ id: `re_${net.resendCalls.length}` }), {
        status: 200,
        headers: { 'content-type': 'application/json' },
      });
    }
    throw new Error(`unexpected fetch in test: ${url}`);
  }) as typeof fetch;

  const env: Env = {
    DB: db,
    FLOODGATE_PREFIX: '.',
    REPLACE_SPACES: 'true',
    NORMALIZATION_VERSION: '1',
    BASE_URL: 'https://whitelist.example',
    SERVER_ADDRESS: 'mine.sambonius.net',
    BEDROCK_PORT: '19132',
    TURNSTILE_SITE_KEY: 'test-site-key',
    EMAIL_FROM: 'Whitelist <whitelist@example.net>',
    TURNSTILE_SECRET: 'test-turnstile-secret',
    RESEND_API_KEY: 'test-resend-key',
    DAEMON_TOKEN: 'daemon-token',
    ADMIN_API_TOKEN: 'admin-token',
  };

  const deps: Deps = {
    now: () => clock.t,
    fetch: stubFetch,
    defer: (p) => deferred.push(p.catch(() => {})),
  };

  return {
    db,
    env,
    deps,
    net,
    clock,
    async flush() {
      while (deferred.length) await deferred.shift();
    },
  };
}

export function submitRequest(fields: Record<string, string>, ip = '203.0.113.5'): Request {
  const fd = new FormData();
  for (const [k, v] of Object.entries(fields)) fd.set(k, v);
  if (!fd.has('cf-turnstile-response')) fd.set('cf-turnstile-response', 'tok');
  return new Request('https://whitelist.example/api/submit', {
    method: 'POST',
    body: fd,
    headers: { 'cf-connecting-ip': ip },
  });
}

export function apiRequest(
  path: string,
  opts: { method?: string; token?: string; body?: unknown } = {},
): Request {
  const headers: Record<string, string> = {};
  if (opts.token) headers.authorization = `Bearer ${opts.token}`;
  let body: string | undefined;
  if (opts.body !== undefined) {
    headers['content-type'] = 'application/json';
    body = JSON.stringify(opts.body);
  }
  return new Request(`https://whitelist.example${path}`, {
    method: opts.method ?? (body ? 'POST' : 'GET'),
    headers,
    body,
  });
}
