import assert from 'node:assert/strict';
import test from 'node:test';
import { validateMonthlyEditPlan, resolveMonthlyTiming } from '../api/edit-plan.js';
const twelve = <T>(value: T): T[] => Array.from({ length: 12 }, () => value);
test('accepts an exact bounded monthly edit plan', () => {
  const plan = validateMonthlyEditPlan({ segment_frames: twelve(202), clip_playback_rates: twelve(1), fade_frames: 30, narration_start_frames: twelve(18), scene_effects: twelve({ zoomStart: 1.03, zoomEnd: 1.09, panXStart: -.01, panXEnd: .01, vignette: .1 }) });
  assert.equal(plan?.segment_frames?.[0], 202);
});
test('rejects malformed effects, nan and invalid timing arrays', () => {
  assert.throws(() => validateMonthlyEditPlan({ scene_effects: twelve({ zoomStart: 1.19 }) }));
  assert.throws(() => validateMonthlyEditPlan({ scene_effects: twelve({ zoomStart: Number.NaN }) }));
  assert.throws(() => validateMonthlyEditPlan({ narration_start_frames: twelve(-1) }));
  assert.throws(() => validateMonthlyEditPlan({ clip_playback_rates: twelve(0) }));
  assert.throws(() => validateMonthlyEditPlan({ unknown: 1 }));
});
test('resolves actual timing and rejects truncation or source overrun', async () => {
  const { resolveMonthlyTiming } = await import('../api/edit-plan.js');
  const source = twelve(6.708333), narration = twelve(3.476);
  const plan = validateMonthlyEditPlan({ segment_frames: twelve(202), clip_playback_rates: twelve(1), fade_frames: 30, narration_start_frames: twelve(18) });
  assert.equal(resolveMonthlyTiming(plan, source, narration, 30).segmentFrames[0], 202);
  assert.throws(() => resolveMonthlyTiming(validateMonthlyEditPlan({ segment_frames: twelve(100), narration_start_frames: twelve(18) }), source, narration, 30));
  assert.throws(() => resolveMonthlyTiming(validateMonthlyEditPlan({ segment_frames: twelve(250) }), source, narration, 30));
  assert.equal(resolveMonthlyTiming(validateMonthlyEditPlan({ segment_frames: twelve(200) }), source, narration, 30).segmentFrames[0], 200);
});

test('optional narration preserves silent video edits and gains stay bounded', () => {
  const timing = resolveMonthlyTiming(undefined, twelve(6.708333), [], 30);
  assert.equal(timing.segmentFrames[0], 202);
  assert.deepEqual(timing.narrationFrames, []);
  const plan = validateMonthlyEditPlan({ narration_volume: 0.72, music_volume: 0.23, music_ducked_volume: 0.12 });
  assert.equal(plan?.narration_volume, 0.72);
  for (const value of [NaN, Infinity, true, -0.1, 1.1]) {
    assert.throws(() => validateMonthlyEditPlan({ music_volume: value }));
  }
  assert.throws(() => resolveMonthlyTiming({ narration_start_frames: twelve(202) }, twelve(6.708333), [], 30));
});
