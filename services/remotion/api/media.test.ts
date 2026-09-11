import test from 'node:test';
import assert from 'node:assert/strict';
import http from 'node:http';
import crypto from 'node:crypto';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { assertRenderedOutput, framesForDuration, stageMedia, validateMediaUrl } from './media.js';
import { RenderManager, storageErrorDiagnostics } from './render.js';

const pcmWav = (samples: number) => {
  const bytes = 44 + samples * 2;
  const wav = Buffer.alloc(bytes);
  wav.write('RIFF', 0); wav.writeUInt32LE(bytes - 8, 4); wav.write('WAVEfmt ', 8);
  wav.writeUInt32LE(16, 16); wav.writeUInt16LE(1, 20); wav.writeUInt16LE(1, 22);
  wav.writeUInt32LE(24000, 24); wav.writeUInt32LE(48000, 28); wav.writeUInt16LE(2, 32); wav.writeUInt16LE(16, 34);
  wav.write('data', 36); wav.writeUInt32LE(samples * 2, 40);
  return wav;
};

test('media URLs require an explicitly trusted HTTPS or loopback origin', () => {
  process.env.REMOTION_MEDIA_ALLOWED_ORIGINS = 'https://assets.example,http://127.0.0.1:8123';
  assert.equal(validateMediaUrl('https://assets.example/clip.mp4').hostname, 'assets.example');
  assert.equal(validateMediaUrl('http://127.0.0.1:8123/clip.mp4').hostname, '127.0.0.1');
  for (const input of ['file:///tmp/clip.mp4', '/tmp/clip.mp4', 'http://assets.example/clip.mp4', 'https://user:pass@assets.example/clip.mp4', 'https://other.example/clip.mp4']) assert.throws(() => validateMediaUrl(input));
});

test('source durations map to a nonzero fixed 30fps timeline', () => {
  assert.equal(framesForDuration(3.375, 30), 101);
  assert.equal(framesForDuration(0.001, 30), 1);
});

test('callback delivery requires exact configured HTTP origins and preserves HTTPS hosts', () => {
  const manager = new RenderManager({
    concurrency: 1,
    outputDir: '/tmp/myloware-callback-test',
    publicBaseUrl: 'https://renderer.example',
    callbackAllowlist: ['http://renderer.internal:8080', 'callbacks.example'],
  });
  const allowed = (url: string) => (manager as any).isCallbackAllowed(url);

  assert.equal(allowed('http://renderer.internal:8080/webhooks/remotion'), true);
  assert.equal(allowed('http://renderer.internal:8081/webhooks/remotion'), false);
  assert.equal(allowed('http://other.internal:8080/webhooks/remotion'), false);
  assert.equal(allowed('https://callbacks.example/webhooks/remotion'), true);
  assert.equal(allowed('https://child.callbacks.example/webhooks/remotion'), true);
});

test('storage failure details are retained in existing media diagnostics', () => {
  const details = Object.freeze({ phase: 'progress', reserve_bytes: 2 * 1024 ** 3 });
  assert.deepEqual(
    storageErrorDiagnostics({ code: 'render_storage_budget_exceeded', details }),
    { code: 'render_storage_budget_exceeded', details },
  );
  assert.equal(storageErrorDiagnostics(new Error('ordinary render failure')), undefined);
  assert.equal(storageErrorDiagnostics(null), undefined);
  assert.equal(storageErrorDiagnostics(undefined), undefined);
  assert.equal(storageErrorDiagnostics('renderer stopped'), undefined);
});

test('renderer preserves the optional studio input hash in its job receipt', async () => {
  const directory = await fs.mkdtemp(path.join(os.tmpdir(), 'myloware-render-hash-'));
  try {
    const manager = new RenderManager({ concurrency: 1, outputDir: directory, publicBaseUrl: 'https://renderer.example' });
    // Queue processing will fail without actual clip URLs, but the accepted job
    // receipt is the correlation boundary and is available immediately.
    const job = await manager.enqueue({ run_id: 'run', input_hash: 'a'.repeat(64), clips: ['https://assets.example/a.mp4'], duration_frames: 300, fps: 30, width: 1080, height: 1920 });
    assert.equal(job.inputHash, 'a'.repeat(64));
    assert.equal(job.renderStatus.schema_version, 1);
    assert.ok(['queued', 'preparing', 'composition', 'failed'].includes(job.renderStatus.phase));
    assert.equal(job.renderStatus.progress, 0);
  } finally {
    await fs.rm(directory, { recursive: true, force: true });
  }
});

test('renderer removes only the app-accepted verified output and records idempotent deletion', async () => {
  const directory = await fs.mkdtemp(path.join(os.tmpdir(), 'myloware-render-cleanup-'));
  const manager = new RenderManager({ concurrency: 1, outputDir: directory, publicBaseUrl: 'https://renderer.example' });
  const id = 'accepted-render-output';
  const runId = 'run-123';
  const inputHash = 'b'.repeat(64);
  const outputPath = path.join(directory, `${id}.mp4`);
  const bytes = Buffer.from('verified renderer bytes');
  const expectedSha256 = crypto.createHash('sha256').update(bytes).digest('hex');
  try {
    await fs.writeFile(outputPath, bytes);
    (manager as any).jobs.set(id, {
      id,
      runId,
      inputHash,
      status: 'done',
      progress: 1,
      outputPath,
      mediaDiagnostics: { verified: true, errors: [] },
      createdAt: Date.now(),
    });

    assert.equal(
      await manager.deleteVerifiedOutput({ id, runId, inputHash, expectedSha256 }),
      'deleted',
    );
    await assert.rejects(fs.stat(outputPath));
    assert.equal(
      await manager.deleteVerifiedOutput({ id, runId, inputHash, expectedSha256 }),
      'already_deleted',
    );
    await assert.rejects(
      manager.deleteVerifiedOutput({ id, runId, inputHash, expectedSha256: 'c'.repeat(64) }),
      /output_not_eligible/,
    );
  } finally {
    await fs.rm(directory, { recursive: true, force: true });
  }
});

test('renderer serializes concurrent identical verified-output cleanup requests', async () => {
  const directory = await fs.mkdtemp(path.join(os.tmpdir(), 'myloware-render-cleanup-concurrent-'));
  const manager = new RenderManager({ concurrency: 1, outputDir: directory, publicBaseUrl: 'https://renderer.example' });
  const id = 'concurrent-render-output';
  const runId = 'run-456';
  const inputHash = 'e'.repeat(64);
  const outputPath = path.join(directory, `${id}.mp4`);
  const bytes = Buffer.from('concurrently accepted renderer bytes');
  const expectedSha256 = crypto.createHash('sha256').update(bytes).digest('hex');
  try {
    await fs.writeFile(outputPath, bytes);
    (manager as any).jobs.set(id, {
      id,
      runId,
      inputHash,
      status: 'done',
      progress: 1,
      outputPath,
      mediaDiagnostics: { verified: true, errors: [] },
      createdAt: Date.now(),
    });
    const request = { id, runId, inputHash, expectedSha256 };
    assert.deepEqual(
      await Promise.all([
        manager.deleteVerifiedOutput(request),
        manager.deleteVerifiedOutput(request),
      ]),
      ['deleted', 'already_deleted'],
    );
    await assert.rejects(fs.lstat(outputPath));
  } finally {
    await fs.rm(directory, { recursive: true, force: true });
  }
});

test('renderer cleanup rejects an active, unverified, mismatched, or unowned output', async () => {
  const directory = await fs.mkdtemp(path.join(os.tmpdir(), 'myloware-render-cleanup-refusal-'));
  const manager = new RenderManager({ concurrency: 1, outputDir: directory, publicBaseUrl: 'https://renderer.example' });
  const id = 'unaccepted-render-output';
  const outputPath = path.join(directory, `${id}.mp4`);
  const expectedSha256 = crypto.createHash('sha256').update('bytes').digest('hex');
  try {
    await fs.writeFile(outputPath, 'bytes');
    const job = {
      id,
      runId: 'run-123',
      inputHash: 'd'.repeat(64),
      status: 'rendering',
      progress: 0,
      outputPath,
      mediaDiagnostics: { verified: true, errors: [] },
      createdAt: Date.now(),
    };
    (manager as any).jobs.set(id, job);
    await assert.rejects(
      manager.deleteVerifiedOutput({ id, runId: job.runId, inputHash: job.inputHash, expectedSha256 }),
      /output_not_eligible/,
    );
    job.status = 'done';
    job.mediaDiagnostics.verified = false;
    await assert.rejects(
      manager.deleteVerifiedOutput({ id, runId: job.runId, inputHash: job.inputHash, expectedSha256 }),
      /output_not_eligible/,
    );
    job.mediaDiagnostics.verified = true;
    job.outputPath = path.join(directory, 'other.mp4');
    await assert.rejects(
      manager.deleteVerifiedOutput({ id, runId: job.runId, inputHash: job.inputHash, expectedSha256 }),
      /output_not_eligible/,
    );
    assert.equal((await fs.readFile(outputPath)).toString(), 'bytes');
  } finally {
    await fs.rm(directory, { recursive: true, force: true });
  }
});

test('final output requires matching 30fps, duration, dimensions, and expected audio', () => {
  const expected = { width: 360, height: 640, fps: 30, frames: 101, expectsAudio: true };
  assert.doesNotThrow(() => assertRenderedOutput({ duration_seconds: 101 / 30, width: 360, height: 640, frame_rate: 30, has_video: true, has_audio: true }, expected));
  assert.throws(() => assertRenderedOutput({ duration_seconds: 2, width: 360, height: 640, frame_rate: 30, has_video: true, has_audio: true }, expected), /duration/);
  assert.throws(() => assertRenderedOutput({ duration_seconds: 101 / 30, width: 360, height: 640, frame_rate: 24, has_video: true, has_audio: true }, expected), /frame rate/);
  assert.throws(() => assertRenderedOutput({ duration_seconds: 101 / 30, width: 360, height: 640, frame_rate: 30, has_video: true, has_audio: false }, expected), /audio/);
});

test('stages and probes a framed WAV, while rejecting an empty WAV', { concurrency: false }, async () => {
  const server = http.createServer((request, response) => {
    response.setHeader('content-type', 'audio/wav');
    response.end(request.url === '/empty.wav' ? pcmWav(0) : pcmWav(240));
  });
  await new Promise<void>((resolve) => server.listen(0, '127.0.0.1', resolve));
  const address = server.address();
  assert.ok(address && typeof address !== 'string');
  const origin = `http://127.0.0.1:${address.port}`;
  process.env.REMOTION_MEDIA_ALLOWED_ORIGINS = origin;
  const directory = await fs.mkdtemp(path.join(os.tmpdir(), 'myloware-media-test-'));
  try {
    const staged = await stageMedia(`${origin}/speech.wav`, directory, 'speech');
    assert.equal(staged.metadata.has_audio, true);
    assert.ok(staged.metadata.duration_seconds > 0);
    await assert.rejects(stageMedia(`${origin}/empty.wav`, directory, 'empty'), /duration is missing or invalid/);
  } finally {
    await fs.rm(directory, { recursive: true, force: true });
    await new Promise<void>((resolve, reject) => server.close((error) => error ? reject(error) : resolve()));
  }
});
