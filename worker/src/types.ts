// Shared types for the Worker half.
//
// Handlers are written against DBLike — the small structural subset of the
// D1 API this project uses — so the test suite can substitute a node:sqlite
// shim and exercise the real SQL (including the partial unique index)
// without a Workers runtime.

export interface DBRow {
  [key: string]: unknown;
}

export interface DBRunMeta {
  changes: number;
  last_row_id: number;
}

export interface DBStatement {
  bind(...values: unknown[]): DBStatement;
  first<T = DBRow>(): Promise<T | null>;
  all<T = DBRow>(): Promise<{ results: T[] }>;
  run(): Promise<{ meta: DBRunMeta }>;
}

export interface DBLike {
  prepare(sql: string): DBStatement;
}

export interface Env {
  DB: DBLike;

  // Normalization pin (§4). Changing these is a migration, not a config edit.
  FLOODGATE_PREFIX: string;
  REPLACE_SPACES: string;
  NORMALIZATION_VERSION: string;

  // Site copy / addresses.
  BASE_URL: string;
  SERVER_ADDRESS: string;
  BEDROCK_PORT: string;
  TURNSTILE_SITE_KEY: string;
  EMAIL_FROM: string;

  // Secrets.
  TURNSTILE_SECRET: string;
  RESEND_API_KEY: string;
  DAEMON_TOKEN: string;
  ADMIN_API_TOKEN: string;
}

// Injectable side effects so tests control time, network, and deferral.
export interface Deps {
  now(): number; // unix epoch seconds
  fetch: typeof fetch;
  defer(p: Promise<unknown>): void; // ctx.waitUntil in production
}

export type Platform = 'java' | 'bedrock';

export interface NormConfig {
  prefix: string;
  replaceSpaces: boolean;
  version: number;
}

export function normConfig(env: Env): NormConfig {
  return {
    prefix: env.FLOODGATE_PREFIX,
    replaceSpaces: env.REPLACE_SPACES === 'true',
    version: parseInt(env.NORMALIZATION_VERSION, 10),
  };
}

// Policy constants (§5).
export const PENDING_TTL_S = 14 * 86400; // pending expires after 14 days
export const VERIFIED_TTL_S = 30 * 86400; // verified-but-undecided expires after 30 days
export const NUDGE_AFTER_S = 48 * 3600; // one nudge for pending with no qualifying attempt
export const RETENTION_S = 90 * 86400; // PII nulled 90 days after terminal_at
export const ATTEMPT_LOOKBACK_S = 3600; // §3 eligibility lookback, re-enforced server-side
export const EMAIL_MAX_ATTEMPTS = 10;
export const RATE_LIMIT_WINDOW_S = 3600;
export const RATE_LIMIT_MAX = 5; // submissions per IP per window
