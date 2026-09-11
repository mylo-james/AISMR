import test from 'node:test';
import assert from 'node:assert/strict';
import crypto from 'node:crypto';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { preserveSceneNarration, resolvePrivateNarrationRetention } from './narration-archive.js';

const provenance = {
  render_preset_id: 'monthly-saved-voice-v2',
  render_preset_sha256: 'a'.repeat(64),
  voice_profile_sha256: 'b'.repeat(64),
  month_bank_id: 'teacup-whisper-months-v1',
  month_bank_sha256: 'c'.repeat(64),
  join_pause_seconds: 0,
};

const pcmWav = (ordinal: number): Buffer => {
  const bytes = 46;
  const wav = Buffer.alloc(bytes);
  wav.write('RIFF', 0); wav.writeUInt32LE(bytes - 8, 4); wav.write('WAVEfmt ', 8);
  wav.writeUInt32LE(16, 16); wav.writeUInt16LE(1, 20); wav.writeUInt16LE(1, 22);
  wav.writeUInt32LE(44100, 24); wav.writeUInt32LE(88200, 28); wav.writeUInt16LE(2, 32); wav.writeUInt16LE(16, 34);
  wav.write('data', 36); wav.writeUInt32LE(2, 40); wav.writeInt16LE(ordinal, 44);
  return wav;
};

test('private narration retention is an explicit development loopback opt-in', () => {
  assert.equal(resolvePrivateNarrationRetention({ preserveNarration: false, nodeEnv: 'production', host: '0.0.0.0' }), false);
  assert.equal(resolvePrivateNarrationRetention({ preserveNarration: true, nodeEnv: 'development', host: '127.0.0.1' }), true);
  assert.throws(() => resolvePrivateNarrationRetention({ preserveNarration: true, nodeEnv: 'production', host: '127.0.0.1' }), /NODE_ENV=development/);
  assert.throws(() => resolvePrivateNarrationRetention({ preserveNarration: true, nodeEnv: 'development', host: '0.0.0.0' }), /loopback HOST/);
});

test('retention copies and verifies all assembled WAVs before staging cleanup', async () => {
  const outputDir = await fs.mkdtemp(path.join(os.tmpdir(), 'myloware-narration-output-'));
  const stagingDir = path.join(outputDir, '.staging', 'scene-job-1');
  await fs.mkdir(stagingDir, { recursive: true });
  try {
    const assembledWavs = await Promise.all(Array.from({ length: 12 }, async (_, index) => {
      const ordinal = index + 1;
      const file = path.join(stagingDir, `narration-${index}-assembled.wav`);
      await fs.writeFile(file, pcmWav(ordinal));
      return { ordinal, path: file };
    }));
    const receipt = await preserveSceneNarration({ enabled: true, outputDir, stagingDir, jobId: 'scene-job-1', provenance, assembledWavs });
    assert.ok(receipt);
    assert.equal(receipt.file_count, 12);
    const archiveDir = path.join(outputDir, 'narration-archive', 'scene-job-1');
    const manifest = JSON.parse(await fs.readFile(path.join(archiveDir, 'provenance.json'), 'utf8'));
    assert.equal(manifest.voice_profile_sha256, provenance.voice_profile_sha256);
    assert.equal(manifest.render_preset_sha256, provenance.render_preset_sha256);
    assert.equal(manifest.assembled_wavs.length, 12);
    for (let index = 0; index < 12; index += 1) {
      const source = await fs.readFile(assembledWavs[index].path);
      const archived = await fs.readFile(path.join(archiveDir, `${String(index + 1).padStart(2, '0')}-assembled.wav`));
      assert.deepEqual(archived, source);
      assert.equal(manifest.assembled_wavs[index].sha256, crypto.createHash('sha256').update(source).digest('hex'));
    }
    await fs.rm(stagingDir, { recursive: true, force: true });
    assert.equal((await fs.readdir(archiveDir)).length, 13);
  } finally {
    await fs.rm(outputDir, { recursive: true, force: true });
  }
});

test('disabled or incomplete narration retention creates no archive', async () => {
  const outputDir = await fs.mkdtemp(path.join(os.tmpdir(), 'myloware-narration-disabled-'));
  const stagingDir = path.join(outputDir, '.staging', 'scene-job-2');
  await fs.mkdir(stagingDir, { recursive: true });
  try {
    assert.equal(await preserveSceneNarration({ enabled: false, outputDir, stagingDir, jobId: 'scene-job-2', provenance, assembledWavs: [] }), undefined);
    await assert.rejects(
      preserveSceneNarration({ enabled: true, outputDir, stagingDir, jobId: 'scene-job-2', provenance, assembledWavs: [] }),
      /twelve ordered/,
    );
    await assert.rejects(fs.stat(path.join(outputDir, 'narration-archive', 'scene-job-2')));
  } finally {
    await fs.rm(outputDir, { recursive: true, force: true });
  }
});

test('an archive copy failure removes the partial archive instead of retaining unverified audio', async () => {
  const outputDir = await fs.mkdtemp(path.join(os.tmpdir(), 'myloware-narration-failure-'));
  const stagingDir = path.join(outputDir, '.staging', 'scene-job-3');
  await fs.mkdir(stagingDir, { recursive: true });
  try {
    const assembledWavs = await Promise.all(Array.from({ length: 12 }, async (_, index) => {
      const ordinal = index + 1;
      const file = path.join(stagingDir, `narration-${index}-assembled.wav`);
      await fs.writeFile(file, pcmWav(ordinal));
      return { ordinal, path: file };
    }));
    await fs.unlink(assembledWavs[8].path);
    await assert.rejects(
      preserveSceneNarration({ enabled: true, outputDir, stagingDir, jobId: 'scene-job-3', provenance, assembledWavs }),
      /ENOENT/,
    );
    await assert.rejects(fs.stat(path.join(outputDir, 'narration-archive', 'scene-job-3')));
  } finally {
    await fs.rm(outputDir, { recursive: true, force: true });
  }
});
