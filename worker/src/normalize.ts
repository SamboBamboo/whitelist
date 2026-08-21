// Normalization (§4): forward only, platform-scoped, never reversed.
//
// The Python half implements the same rules; both must pass
// shared/normalization-fixtures.json. Trim is ASCII-only so JS and Python
// agree byte-for-byte. Log-sourced input is never trimmed — the log token
// is exact, and Bedrock names may legitimately contain spaces when
// replace-spaces is off.

import type { Platform } from './types.ts';

export type NormError = 'empty' | 'prefix_missing';
export type NormResult =
  | { ok: true; normalized: string }
  | { ok: false; error: NormError };

export interface NormSettings {
  prefix: string;
  replaceSpaces: boolean;
}

const EDGE_ASCII_WS = /^[ \t\r\n\f\v]+|[ \t\r\n\f\v]+$/g;

export function asciiTrim(s: string): string {
  return s.replace(EDGE_ASCII_WS, '');
}

// Form input: what the applicant typed. Apply Floodgate's transformation
// forward (trim → replace spaces → lowercase) for Bedrock; trim + lowercase
// for Java. Underscores are real characters and are never collapsed.
export function normalizeForm(
  platform: Platform,
  input: string,
  cfg: NormSettings,
): NormResult {
  let s = asciiTrim(input);
  if (platform === 'bedrock' && cfg.replaceSpaces) {
    s = s.split(' ').join('_');
  }
  s = s.toLowerCase();
  if (s === '') return { ok: false, error: 'empty' };
  return { ok: true, normalized: s };
}

// Logged input: the exact token the server printed. Verify and strip the
// configured Floodgate prefix for Bedrock, lowercase, nothing else. Never
// convert underscores back into spaces — the mapping is one-way and lossy.
export function normalizeLogged(
  platform: Platform,
  raw: string,
  cfg: NormSettings,
): NormResult {
  let s = raw;
  if (platform === 'bedrock' && cfg.prefix !== '') {
    if (!s.startsWith(cfg.prefix)) return { ok: false, error: 'prefix_missing' };
    s = s.slice(cfg.prefix.length);
  }
  s = s.toLowerCase();
  if (s === '') return { ok: false, error: 'empty' };
  return { ok: true, normalized: s };
}
