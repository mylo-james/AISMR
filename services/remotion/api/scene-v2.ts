import { loadMonthBank, validateSceneV2, type SceneV2 } from './month-bank.js';

export type PreparedSceneV2 = {
  scenes: SceneV2[];
  preset: { id: string; sha256: string };
  voice_profile_digest: string;
  voice_profile: Record<string, unknown>;
};

/** Route ingress seam: validate and project editor's scene-only payload. */
export const prepareSceneV2 = async (body: Record<string, unknown>): Promise<PreparedSceneV2 | undefined> => {
  if (body.template !== 'monthly-scene-v2') return undefined;
  const scenes = validateSceneV2(body.ordered_scenes);
  const preset = body.render_preset as { id?: unknown; sha256?: unknown } | undefined;
  await loadMonthBank({
    presetId: preset?.id,
    presetSha256: preset?.sha256,
    voiceProfileDigest: body.voice_profile_digest,
    voiceProfile: body.voice_profile,
  });
  if (!preset || typeof preset.id !== 'string' || typeof preset.sha256 !== 'string' || typeof body.voice_profile_digest !== 'string' || !body.voice_profile || typeof body.voice_profile !== 'object' || Array.isArray(body.voice_profile)) throw new Error('scene-v2 preset unavailable');
  body.template = 'monthly';
  body.clips = scenes.map((scene) => scene.video_ref);
  body.objects = scenes.map((scene) => scene.title);
  body.narration_urls = scenes.map((scene) => scene.title_audio_ref);
  return { scenes, preset: { id: preset.id, sha256: preset.sha256 }, voice_profile_digest: body.voice_profile_digest, voice_profile: body.voice_profile as Record<string, unknown> };
};
