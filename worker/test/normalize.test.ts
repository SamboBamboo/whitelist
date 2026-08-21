// Normalization fixtures (§4) — the TypeScript side of the shared contract.

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { normalizeForm, normalizeLogged } from '../src/normalize.ts';
import type { Platform } from '../src/types.ts';

interface FixtureCase {
  id: string;
  platform: Platform;
  source: 'form' | 'log';
  input: string;
  config: { username_prefix: string; replace_spaces: boolean };
  expect: { ok: true; normalized: string } | { ok: false; error: string };
}

interface PairRef {
  platform: Platform;
  source: 'form' | 'log';
  input: string;
}

const fixtures = JSON.parse(
  readFileSync(new URL('../../shared/normalization-fixtures.json', import.meta.url), 'utf8'),
) as {
  normalization_version: number;
  cases: FixtureCase[];
  distinct_pairs: { a: PairRef; b: PairRef }[];
  colliding_pairs: { a: PairRef; b: PairRef }[];
};

function run(c: { platform: Platform; source: string; input: string }, cfg: {
  username_prefix: string;
  replace_spaces: boolean;
}) {
  const settings = { prefix: cfg.username_prefix, replaceSpaces: cfg.replace_spaces };
  return c.source === 'form'
    ? normalizeForm(c.platform, c.input, settings)
    : normalizeLogged(c.platform, c.input, settings);
}

test('shared fixtures all pass', () => {
  for (const c of fixtures.cases) {
    const got = run(c, c.config);
    if (c.expect.ok) {
      assert.deepEqual(got, { ok: true, normalized: c.expect.normalized }, c.id);
    } else {
      assert.deepEqual(got, { ok: false, error: c.expect.error }, c.id);
    }
  }
});

const DEFAULT_CFG = { username_prefix: '.', replace_spaces: true };

test('distinct pairs stay distinct (underscores are real)', () => {
  for (const { a, b } of fixtures.distinct_pairs) {
    const ra = run(a, DEFAULT_CFG);
    const rb = run(b, DEFAULT_CFG);
    assert.ok(ra.ok && rb.ok);
    assert.notEqual(ra.normalized, rb.normalized);
  }
});

test('colliding pairs collide (Floodgate lossy mapping, surfaced not hidden)', () => {
  for (const { a, b } of fixtures.colliding_pairs) {
    const ra = run(a, DEFAULT_CFG);
    const rb = run(b, DEFAULT_CFG);
    assert.ok(ra.ok && rb.ok);
    assert.equal(ra.normalized, rb.normalized);
  }
});
