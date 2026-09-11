import test from 'node:test';
import assert from 'node:assert/strict';
import { parseFrameConcurrency } from './frame-concurrency.mjs';

test('integer frame concurrency stays numeric for Remotion', () => {
  assert.equal(parseFrameConcurrency(undefined), 1);
  assert.equal(parseFrameConcurrency('1'), 1);
  assert.equal(parseFrameConcurrency(' 4 '), 4);
});

test('valid percentages remain percentages', () => {
  assert.equal(parseFrameConcurrency('100%'), '100%');
  assert.equal(parseFrameConcurrency('25%'), '25%');
});

test('invalid, zero, fractional, and malformed values fail early', () => {
  for (const value of ['0', '0%', '-1', '1.5', '101%', '999999999999999999999999', 'many']) {
    assert.throws(() => parseFrameConcurrency(value), /REMOTION_FRAME_CONCURRENCY/);
  }
});

import { parsePositiveInteger } from './runtime-config.mjs';

test('render-job concurrency accepts only positive finite integers', () => {
  assert.equal(parsePositiveInteger('1', 'REMOTION_JOB_CONCURRENCY'), 1);
  assert.equal(parsePositiveInteger(' 3 ', 'REMOTION_JOB_CONCURRENCY'), 3);
  for (const value of ['0', '-1', '1.5', 'Infinity', 'many']) {
    assert.throws(() => parsePositiveInteger(value, 'REMOTION_JOB_CONCURRENCY'), /REMOTION_JOB_CONCURRENCY/);
  }
});
