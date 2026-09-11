import test from 'node:test';
import assert from 'node:assert/strict';
import {
  REMOTION_MIN_TEMP_FRAME_BUDGET_BYTES,
  REMOTION_STORAGE_RESERVE_BYTES,
  REMOTION_STORAGE_WRITE_HEADROOM_BYTES,
  RenderStorageBudgetError,
  createRenderStorageGuard,
  monitorRenderStorageProgress,
  estimateTempFrameBudgetBytes,
  renderStatusFromNativeProgress,
  renderVideo,
} from '../render.mjs';

test('monthly 2,424-frame JPEG estimate uses the observed-data 512 MiB heuristic floor', () => {
  assert.equal(estimateTempFrameBudgetBytes(2424), REMOTION_MIN_TEMP_FRAME_BUDGET_BYTES);
});

test('renderVideo applies the storage guard before bundle or browser preparation', async () => {
  const error = new RenderStorageBudgetError({
    phase: 'preflight',
    freeBytes: 0,
    requiredBytes: 1,
    reserveBytes: 1,
    writeHeadroomBytes: 0,
    tempBudgetBytes: 0,
  });
  let guardCalls = 0;
  await assert.rejects(
    renderVideo({
      jobId: 'storage-guard-before-bundle',
      clips: [],
      durationFrames: 1,
      fps: 30,
      width: 2,
      height: 2,
      outputPath: '/tmp/not-created.mp4',
      storageGuardFactory: () => ({
        assertCanStart: () => { guardCalls += 1; throw error; },
        checkProgress: () => undefined,
      }),
    }),
    (received) => received === error,
  );
  assert.equal(guardCalls, 1);
});

test('storage guard prevents a render that cannot retain its reserve and working-set budget', () => {
  const budget = estimateTempFrameBudgetBytes(2424);
  const guard = createRenderStorageGuard({
    durationFrames: 2424,
    freeBytes: () => REMOTION_STORAGE_RESERVE_BYTES + REMOTION_STORAGE_WRITE_HEADROOM_BYTES + budget - 1,
  });
  assert.throws(() => guard.assertCanStart(), (error) => {
    assert.ok(error instanceof RenderStorageBudgetError);
    assert.equal(error.code, 'render_storage_budget_exceeded');
    assert.deepEqual(error.details, {
      phase: 'preflight',
      free_bytes: REMOTION_STORAGE_RESERVE_BYTES + REMOTION_STORAGE_WRITE_HEADROOM_BYTES + budget - 1,
      required_bytes: REMOTION_STORAGE_RESERVE_BYTES + REMOTION_STORAGE_WRITE_HEADROOM_BYTES + budget,
      reserve_bytes: REMOTION_STORAGE_RESERVE_BYTES,
      write_headroom_bytes: REMOTION_STORAGE_WRITE_HEADROOM_BYTES,
      temp_frame_budget_bytes: budget,
    });
    return true;
  });
});

test('storage guard retains a structured reason when progress reaches the reserve floor', () => {
  const budget = estimateTempFrameBudgetBytes(2424);
  const guard = createRenderStorageGuard({
    durationFrames: 2424,
    freeBytes: () => REMOTION_STORAGE_RESERVE_BYTES + REMOTION_STORAGE_WRITE_HEADROOM_BYTES - 1,
  });
  const error = guard.checkProgress();
  assert.ok(error instanceof RenderStorageBudgetError);
  assert.equal(error.details.phase, 'progress');
  assert.equal(error.details.required_bytes, REMOTION_STORAGE_RESERVE_BYTES + REMOTION_STORAGE_WRITE_HEADROOM_BYTES);
  assert.equal(error.details.temp_frame_budget_bytes, budget);
  assert.equal(guard.failure, error);
});

test('progress monitoring cancels through Remotion while preserving the typed reason', () => {
  const guard = createRenderStorageGuard({
    durationFrames: 2424,
    freeBytes: () => REMOTION_STORAGE_RESERVE_BYTES + REMOTION_STORAGE_WRITE_HEADROOM_BYTES - 1,
  });
  let cancellations = 0;
  let reportedProgress;
  const error = monitorRenderStorageProgress({
    guard,
    cancel: () => { cancellations += 1; },
    onProgress: (progress) => { reportedProgress = progress; },
    progress: 0.5,
  });
  assert.ok(error instanceof RenderStorageBudgetError);
  assert.equal(cancellations, 1);
  assert.equal(reportedProgress, 0.5);
  assert.equal(error.details.phase, 'progress');
});

test('storage estimate rejects an unsafe frame count', () => {
  assert.throws(() => estimateTempFrameBudgetBytes(0), /positive safe integer/);
});

test('native render progress retains rendered and encoded counters without implying verification', () => {
  assert.deepEqual(
    renderStatusFromNativeProgress({
      progress: 0.4,
      renderedFrames: 120,
      encodedFrames: 0,
      stitchStage: 'encoding',
      totalFrames: 300,
    }),
    {
      phase: 'frames',
      progress: 0.4,
      total_frames: 300,
      rendered_frames: 120,
      encoded_frames: 0,
      stitch_stage: 'encoding',
    },
  );
  assert.equal(
    renderStatusFromNativeProgress({
      progress: 0.2,
      renderedFrames: 60,
      encodedFrames: 0,
      stitchStage: undefined,
      totalFrames: 300,
    }).phase,
    'frames',
  );
  assert.equal(
    renderStatusFromNativeProgress({
      progress: 0.9,
      renderedFrames: 300,
      encodedFrames: 180,
      stitchStage: 'muxing',
      totalFrames: 300,
    }).phase,
    'encoding',
  );
});
