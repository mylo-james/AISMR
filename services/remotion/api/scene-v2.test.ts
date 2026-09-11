import assert from 'node:assert/strict';
import test from 'node:test';
import path from 'node:path';

process.env.REMOTION_ASSET_ROOT = path.resolve(process.cwd(), '..', '..', 'data', 'media', 'voice');
const { prepareSceneV2 } = await import('./scene-v2.js');

const profile = {
  profile_version: 'eleven-v3-whisper-en-v1', model: 'fal-ai/elevenlabs/tts/eleven-v3', voice: 'KmnvDXRA0HU55Q0aqkPG', stability: 0.5,
  language_code: 'en', apply_text_normalization: 'off', timestamps: true, delivery_prefix: '[whispers]', between_cues: '[long pause]',
};
const payload = () => ({
  template: 'monthly-scene-v2',
  ordered_scenes: Array.from({ length: 12 }, (_value, index) => ({ ordinal: index + 1, title: `Proof scene ${index + 1}`, video_ref: `https://media.example/${index + 1}.mp4`, title_audio_ref: `https://media.example/${index + 1}.wav` })),
  render_preset: { id: 'monthly-saved-voice-v2', sha256: '814c8de9532f053160308a273cdfdaac3837053ecc91f150740eb9c1dd9704f6' },
  voice_profile_digest: '99bb477d104635a8dae416d7e90a2ae35402a0ac19fa939b48f49674918e6d3a',
  voice_profile: profile,
});

test('route ingress accepts the exact editor-shaped scene-v2 payload and derives legacy renderer arrays', async () => {
  const request = payload() as Record<string, any>;
  assert.equal('clips' in request || 'objects' in request || 'narration_urls' in request, false);
  const prepared = await prepareSceneV2(request);
  assert.ok(prepared);
  assert.equal(request.template, 'monthly');
  assert.deepEqual(request.clips, request.ordered_scenes.map((scene: { video_ref: string }) => scene.video_ref));
  assert.deepEqual(request.objects, request.ordered_scenes.map((scene: { title: string }) => scene.title));
  assert.deepEqual(request.narration_urls, request.ordered_scenes.map((scene: { title_audio_ref: string }) => scene.title_audio_ref));
});

test('route ingress rejects an altered opaque preset before media staging', async () => {
  const request = payload() as Record<string, any>;
  request.render_preset.sha256 = '0'.repeat(64);
  await assert.rejects(() => prepareSceneV2(request), /preset/);
});
