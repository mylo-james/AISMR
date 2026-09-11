import assert from 'node:assert/strict';
import test from 'node:test';
import path from 'node:path';
import fs from 'node:fs/promises';

process.env.REMOTION_ASSET_ROOT = path.resolve(process.cwd(), '..', '..', 'data', 'media', 'voice');
const { assembleMonthTitleWav, loadMonthBank, validateSceneV2 } = await import('./month-bank.js');

const profile = {
  profile_version: 'eleven-v3-whisper-en-v1', model: 'fal-ai/elevenlabs/tts/eleven-v3', voice: 'KmnvDXRA0HU55Q0aqkPG', stability: 0.5,
  language_code: 'en', apply_text_normalization: 'off', timestamps: true, delivery_prefix: '[whispers]', between_cues: '[long pause]',
};
const digest = '99bb477d104635a8dae416d7e90a2ae35402a0ac19fa939b48f49674918e6d3a';

test('loads the approved immutable month bank only with its pinned preset and profile', async () => {
  const bank = await loadMonthBank({ presetId: 'monthly-saved-voice-v2', presetSha256: '814c8de9532f053160308a273cdfdaac3837053ecc91f150740eb9c1dd9704f6', voiceProfileDigest: digest, voiceProfile: profile });
  assert.equal(bank.manifestSha256, '41b05666f6fcf7ca2c68c88ee7bfe36ae7463c727154c7633eefe13d5ed7e0df');
  assert.deepEqual(bank.manifest.clips.map((clip) => clip.label), ['January', 'February', 'March', 'April', 'May', 'June', 'July', 'August', 'September', 'October', 'November', 'December']);
});

test('rejects non-ordered scene arrays before a renderer can map months', () => {
  assert.throws(() => validateSceneV2([{ ordinal: 2, title: 'X', video_ref: 'https://x', title_audio_ref: 'https://x' }]));
});

test('assembles local PCM with the preset zero-length inter-phrase pause', async () => {
  const bank = await loadMonthBank({ presetId: 'monthly-saved-voice-v2', presetSha256: '814c8de9532f053160308a273cdfdaac3837053ecc91f150740eb9c1dd9704f6', voiceProfileDigest: digest, voiceProfile: profile });
  const directory = await fs.mkdtemp(path.join(process.cwd(), '.month-bank-test-'));
  try {
    const title = path.join(bank.bankDir, bank.manifest.clips[1].filename);
    const result = await assembleMonthTitleWav({ bank, index: 0, titleAudioPath: title, directory });
    assert.equal(result.metadata.sample_rate, 44100);
    assert.equal(result.metadata.has_audio, true);
    assert.ok(result.metadata.duration_seconds > 6.3 && result.metadata.duration_seconds < 6.4);
  } finally {
    await fs.rm(directory, { recursive: true, force: true });
  }
});
