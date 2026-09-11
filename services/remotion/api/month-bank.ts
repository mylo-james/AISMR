import crypto from 'node:crypto';
import path from 'node:path';
import fs from 'node:fs/promises';
import { existsSync } from 'node:fs';
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { fileURLToPath } from 'node:url';
import { probeOutputFile, type StagedMedia } from './media.js';

const execFileAsync = promisify(execFile);
const FFMPEG_PATH = process.env.FFMPEG_PATH || 'ffmpeg';
const SHA256 = /^[a-f0-9]{64}$/;
const OPAQUE_ID = /^[a-z][a-z0-9-]{0,95}$/;
const MONTH_COUNT = 12;

type BankClip = { index: number; ordinal: number; label: string; filename: string; sha256: string; byte_size: number };
type BankManifest = { schema_version: number; bank_id: string; review_status: string; voice_profile: Record<string, unknown>; voice_profile_sha256: string; audio_format: { container: string; codec: string; sample_rate_hz: number; channels: number; bits_per_sample: number }; clips: BankClip[] };
type Preset = { schema_version: number; id: string; render_contract: string; template: string; scene_count: number; month_bank_id: string; month_bank_sha256: string; voice_profile_sha256: string; join_pause_seconds: number; segment_frames: number; fps: number; narration_start_frames: number };
export type SceneV2 = { ordinal: number; title: string; video_ref: string; title_audio_ref: string };
export type ValidatedMonthBank = { preset: Preset; manifest: BankManifest; bankDir: string; manifestSha256: string; clipBytes: Buffer[] };

const sha256 = (value: Buffer | string): string => crypto.createHash('sha256').update(value).digest('hex');
const canonical = (value: unknown): string => {
  if (value === null || typeof value !== 'object') return JSON.stringify(value);
  if (Array.isArray(value)) return `[${value.map(canonical).join(',')}]`;
  const object = value as Record<string, unknown>;
  return `{${Object.keys(object).sort().map((key) => `${JSON.stringify(key)}:${canonical(object[key])}`).join(',')}}`;
};
const readJson = async <T>(file: string): Promise<{ raw: Buffer; value: T }> => {
  const raw = await fs.readFile(file);
  return { raw, value: JSON.parse(raw.toString('utf8')) as T };
};

const moduleDir = path.dirname(fileURLToPath(import.meta.url));
const assetRoot = (): string => {
  if (process.env.REMOTION_ASSET_ROOT) return process.env.REMOTION_ASSET_ROOT;
  const candidates = [
    path.resolve(process.cwd(), 'data', 'media', 'voice'),
    // `npm build` copies immutable assets alongside `dist/api`.
    path.resolve(moduleDir, '..', 'data', 'media', 'voice'),
    path.resolve(moduleDir, '..', '..', '..', 'data', 'media', 'voice'),
    // Local launcher starts dist/api from its isolated runtime data directory.
    path.resolve(moduleDir, '..', '..', '..', '..', 'data', 'media', 'voice'),
  ];
  return candidates.find((candidate) => existsSync(candidate)) ?? candidates[0];
};
const presetPath = (id: string): string => path.join(assetRoot(), 'presets', `${id}.json`);
const bankDir = (id: string): string => path.join(assetRoot(), 'months', id);

export const validateSceneV2 = (scenes: unknown): SceneV2[] => {
  if (!Array.isArray(scenes) || scenes.length !== MONTH_COUNT) throw new Error('scene-v2 requires exactly 12 ordered scenes');
  const titles = new Set<string>();
  return scenes.map((scene, index) => {
    if (!scene || typeof scene !== 'object' || Array.isArray(scene)) throw new Error(`scene ${index + 1} is invalid`);
    const value = scene as Record<string, unknown>;
    if (Object.keys(value).length !== 4 || Object.keys(value).some((key) => !['ordinal', 'title', 'video_ref', 'title_audio_ref'].includes(key)) || value.ordinal !== index + 1 || typeof value.title !== 'string' || !value.title.trim() || value.title.trim().length > 160 || typeof value.video_ref !== 'string' || !value.video_ref.trim() || typeof value.title_audio_ref !== 'string' || !value.title_audio_ref.trim()) throw new Error(`scene ${index + 1} is invalid`);
    const title = value.title.trim();
    if (titles.has(title.toLocaleLowerCase())) throw new Error('scene-v2 titles must be distinct');
    titles.add(title.toLocaleLowerCase());
    return { ordinal: index + 1, title, video_ref: value.video_ref, title_audio_ref: value.title_audio_ref };
  });
};

export const loadMonthBank = async ({ presetId, presetSha256, voiceProfileDigest, voiceProfile }: { presetId: unknown; presetSha256: unknown; voiceProfileDigest: unknown; voiceProfile: unknown }): Promise<ValidatedMonthBank> => {
  if (typeof presetId !== 'string' || !OPAQUE_ID.test(presetId) || typeof presetSha256 !== 'string' || !SHA256.test(presetSha256) || typeof voiceProfileDigest !== 'string' || !SHA256.test(voiceProfileDigest) || !voiceProfile || typeof voiceProfile !== 'object' || Array.isArray(voiceProfile)) throw new Error('scene-v2 preset or voice profile is invalid');
  const presetFile = presetPath(presetId);
  const { raw: presetRaw, value: preset } = await readJson<Preset>(presetFile);
  if (sha256(presetRaw) !== presetSha256 || preset.schema_version !== 1 || preset.id !== presetId || !OPAQUE_ID.test(preset.month_bank_id) || preset.render_contract !== 'scene-v2' || preset.template !== 'monthly' || preset.scene_count !== MONTH_COUNT || preset.fps !== 30 || preset.segment_frames !== 202 || preset.narration_start_frames !== 18 || !Number.isFinite(preset.join_pause_seconds) || preset.join_pause_seconds < 0 || preset.join_pause_seconds > 1) throw new Error('scene-v2 render preset is unavailable');
  if (preset.voice_profile_sha256 !== voiceProfileDigest || sha256(canonical(voiceProfile)) !== voiceProfileDigest) throw new Error('scene-v2 voice profile does not match its digest');
  const directory = bankDir(preset.month_bank_id);
  const { raw: manifestRaw, value: manifest } = await readJson<BankManifest>(path.join(directory, 'manifest.json'));
  const manifestSha256 = sha256(manifestRaw);
  if (manifestSha256 !== preset.month_bank_sha256 || manifest.schema_version !== 1 || manifest.review_status !== 'approved' || manifest.bank_id !== preset.month_bank_id || !OPAQUE_ID.test(manifest.bank_id) || manifest.voice_profile_sha256 !== voiceProfileDigest || canonical(manifest.voice_profile) !== canonical(voiceProfile) || !manifest.audio_format || manifest.audio_format.container !== 'wav' || manifest.audio_format.codec !== 'pcm_s16le' || manifest.audio_format.sample_rate_hz !== 44100 || manifest.audio_format.channels !== 1 || manifest.audio_format.bits_per_sample !== 16 || !Array.isArray(manifest.clips) || manifest.clips.length !== MONTH_COUNT) throw new Error('scene-v2 month bank is unavailable or incompatible');
  const clipBytes: Buffer[] = [];
  for (let index = 0; index < MONTH_COUNT; index += 1) {
    const clip = manifest.clips[index];
    if (!clip || clip.index !== index || clip.ordinal !== index + 1 || typeof clip.label !== 'string' || !clip.label || typeof clip.filename !== 'string' || !/^[a-z]+\.wav$/.test(clip.filename) || !SHA256.test(clip.sha256) || !Number.isInteger(clip.byte_size) || clip.byte_size <= 44) throw new Error('scene-v2 month bank manifest is invalid');
    const file = path.join(directory, clip.filename);
    const bytes = await fs.readFile(file);
    if (bytes.length !== clip.byte_size || sha256(bytes) !== clip.sha256) throw new Error(`scene-v2 month asset ${index + 1} failed integrity verification`);
    clipBytes.push(bytes);
  }
  return { preset, manifest, bankDir: directory, manifestSha256, clipBytes };
};

export const assembleMonthTitleWav = async ({ bank, index, titleAudioPath, directory }: { bank: ValidatedMonthBank; index: number; titleAudioPath: string; directory: string }): Promise<StagedMedia> => {
  const month = bank.manifest.clips[index];
  if (!Number.isInteger(index) || index < 0 || index >= MONTH_COUNT || !bank.clipBytes[index]) throw new Error('month bank index is invalid');
  const monthInput = path.join(directory, `month-bank-${index}.wav`);
  // Assemble from the bytes verified during bank admission, never by reopening
  // a mutable product path after the integrity check.
  await fs.writeFile(monthInput, bank.clipBytes[index], { flag: 'wx', mode: 0o600 });
  const output = path.join(directory, `narration-${index}-assembled.wav`);
  const inputs = ['-i', monthInput];
  if (bank.preset.join_pause_seconds > 0) inputs.push('-f', 'lavfi', '-t', String(bank.preset.join_pause_seconds), '-i', 'anullsrc=r=44100:cl=mono');
  inputs.push('-i', titleAudioPath);
  const inputCount = bank.preset.join_pause_seconds > 0 ? 3 : 2;
  const labels = Array.from({ length: inputCount }, (_value, item) => `[${item}:a]`).join('');
  await execFileAsync(FFMPEG_PATH, ['-v', 'error', '-y', ...inputs, '-filter_complex', `${labels}concat=n=${inputCount}:v=0:a=1`, '-ar', '44100', '-ac', '1', '-c:a', 'pcm_s16le', output], { timeout: 30_000, maxBuffer: 1024 * 1024 });
  const bytes = (await fs.stat(output)).size;
  return { localPath: output, filename: path.basename(output), bytes, metadata: await probeOutputFile(output) };
};
