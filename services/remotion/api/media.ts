import path from 'node:path';
import fs from 'node:fs/promises';
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';

const execFileAsync = promisify(execFile);
const FFPROBE_PATH = process.env.FFPROBE_PATH || 'ffprobe';
const FFMPEG_PATH = process.env.FFMPEG_PATH || 'ffmpeg';
const PROBE_TIMEOUT_MS = Math.max(1_000, Number(process.env.REMOTION_PROBE_TIMEOUT_MS ?? 15_000));
const FETCH_TIMEOUT_MS = Math.max(1_000, Number(process.env.REMOTION_MEDIA_FETCH_TIMEOUT_MS ?? 30_000));
const MAX_MEDIA_BYTES = Math.max(1_024, Number(process.env.REMOTION_MAX_MEDIA_BYTES ?? 64 * 1024 * 1024));
const AUDIO_TAIL_TOLERANCE_SECONDS = 0.05;

export type MediaMetadata = {
  duration_seconds: number;
  width?: number;
  height?: number;
  frame_rate?: number;
  has_video: boolean;
  has_audio: boolean;
  sample_rate?: number;
  codec?: string;
};
export type StagedMedia = { localPath: string; filename: string; bytes: number; metadata: MediaMetadata };
export class MediaValidationError extends Error {}

export const validateMediaUrl = (rawUrl: string): URL => {
  let parsed: URL;
  try { parsed = new URL(rawUrl); } catch { throw new MediaValidationError('media URL must be absolute HTTP(S)'); }
  const host = parsed.hostname.toLowerCase();
  if (parsed.protocol !== 'https:' && !(parsed.protocol === 'http:' && (host === 'localhost' || host === '127.0.0.1'))) throw new MediaValidationError('media URL must use HTTPS, or HTTP localhost for local development');
  if (parsed.username || parsed.password || !parsed.hostname) throw new MediaValidationError('media URL has an unsupported authority');
  const allowedOrigins = (process.env.REMOTION_MEDIA_ALLOWED_ORIGINS ?? '').split(',').map((item) => item.trim()).filter(Boolean);
  if (allowedOrigins.length === 0) throw new MediaValidationError('REMOTION_MEDIA_ALLOWED_ORIGINS must name trusted media origins');
  if (!allowedOrigins.includes(parsed.origin)) throw new MediaValidationError('media origin is not allowlisted');
  return parsed;
};

const extensionFor = (url: URL, contentType: string | null): string => {
  const ext = path.extname(url.pathname).toLowerCase();
  if (['.mp4', '.mov', '.m4v', '.webm', '.wav', '.mp3', '.m4a', '.aac', '.ogg'].includes(ext)) return ext;
  if (contentType?.startsWith('audio/wav')) return '.wav';
  if (contentType?.startsWith('audio/mpeg')) return '.mp3';
  if (contentType?.startsWith('audio/')) return '.audio';
  if (contentType?.startsWith('video/')) return '.video';
  throw new MediaValidationError('media response must have an ordinary audio or video content type');
};

const fetchMedia = async (rawUrl: string): Promise<{ response: Response; url: URL; finish: () => void }> => {
  const url = validateMediaUrl(rawUrl);
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), FETCH_TIMEOUT_MS);
  try {
    const response = await fetch(url, { redirect: 'manual', signal: controller.signal });
    if (response.status >= 300 && response.status < 400) {
      throw new MediaValidationError('media redirects are not allowed');
    }
    if (!response.ok || !response.body) throw new MediaValidationError(`media download failed with HTTP ${response.status}`);
    const declared = Number(response.headers.get('content-length') ?? 0);
    if (Number.isFinite(declared) && declared > MAX_MEDIA_BYTES) throw new MediaValidationError('media download exceeds size limit');
    return { response, url, finish: () => clearTimeout(timer) };
  } catch (error) {
    clearTimeout(timer);
    throw error;
  }
};

export const stageMedia = async (rawUrl: string, directory: string, basename: string): Promise<StagedMedia> => {
  const { response, url, finish } = await fetchMedia(rawUrl);
  const extension = extensionFor(url, response.headers.get('content-type'));
  const filename = `${basename}${extension}`;
  const localPath = path.join(directory, filename);
  await fs.mkdir(directory, { recursive: true });
  const handle = await fs.open(localPath, 'w');
  let bytes = 0;
  let reader: ReadableStreamDefaultReader<Uint8Array> | undefined;
  try {
    reader = response.body!.getReader();
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      bytes += value.byteLength;
      if (bytes > MAX_MEDIA_BYTES) throw new MediaValidationError('media download exceeds size limit');
      await handle.write(value);
    }
  } catch (error) {
    await reader?.cancel();
    await fs.rm(localPath, { force: true });
    throw error;
  } finally { await handle.close(); finish(); }
  return { localPath, filename, bytes, metadata: await probeOutputFile(localPath) };
};

const parseRate = (value?: string): number | undefined => {
  if (!value || value === '0/0') return undefined;
  const [numerator, denominator] = value.split('/').map(Number);
  const rate = denominator ? numerator / denominator : Number(value);
  return Number.isFinite(rate) && rate > 0 ? rate : undefined;
};
const runProbe = async (input: string): Promise<MediaMetadata> => {
  let stdout: string;
  try {
    ({ stdout } = await execFileAsync(FFPROBE_PATH, ['-v', 'error', '-protocol_whitelist', 'file', '-format_whitelist', 'mov,mp4,m4a,3gp,3g2,mj2,wav,mp3,ogg,webm,matroska', '-show_entries', 'format=duration:stream=codec_type,codec_name,width,height,sample_rate,avg_frame_rate,r_frame_rate', '-of', 'json', input], { timeout: PROBE_TIMEOUT_MS, maxBuffer: 1024 * 1024 }));
  } catch (error) { throw new MediaValidationError(`media probe failed: ${error instanceof Error ? error.message : String(error)}`); }
  let parsed: { format?: { duration?: string }; streams?: Array<Record<string, string>> };
  try { parsed = JSON.parse(stdout); } catch { throw new MediaValidationError('media probe returned invalid JSON'); }
  const duration = Number(parsed.format?.duration);
  if (!Number.isFinite(duration) || duration <= 0) throw new MediaValidationError('media duration is missing or invalid');
  const video = parsed.streams?.find((stream) => stream.codec_type === 'video');
  const audio = parsed.streams?.find((stream) => stream.codec_type === 'audio');
  return { duration_seconds: duration, width: video?.width ? Number(video.width) : undefined, height: video?.height ? Number(video.height) : undefined, frame_rate: parseRate(video?.avg_frame_rate) ?? parseRate(video?.r_frame_rate), has_video: Boolean(video), has_audio: Boolean(audio), sample_rate: audio?.sample_rate ? Number(audio.sample_rate) : undefined, codec: video?.codec_name ?? audio?.codec_name };
};
export const probeOutputFile = async (outputPath: string): Promise<MediaMetadata> => runProbe(outputPath);
export const verifyOutputDecode = async (outputPath: string): Promise<void> => {
  try {
    const { stderr } = await execFileAsync(FFMPEG_PATH, ['-v', 'error', '-xerror', '-i', outputPath, '-f', 'null', '-'], { timeout: PROBE_TIMEOUT_MS, maxBuffer: 1024 * 1024 });
    if (stderr.trim()) throw new MediaValidationError(`output decode verification failed: ${stderr.trim()}`);
  }
  catch (error) { throw new MediaValidationError(`output decode verification failed: ${error instanceof Error ? error.message : String(error)}`); }
};
export const framesForDuration = (seconds: number, fps: number): number => Math.max(1, Math.round(seconds * fps));
export const assertRenderedOutput = (metadata: MediaMetadata, expected: { width: number; height: number; fps: number; frames: number; expectsAudio: boolean }): void => {
  if (!metadata.has_video || metadata.width !== expected.width || metadata.height !== expected.height) throw new MediaValidationError('rendered output dimensions or video stream are invalid');
  if (!metadata.frame_rate || Math.abs(metadata.frame_rate - expected.fps) > 0.01) throw new MediaValidationError('rendered output frame rate is invalid');
  if (expected.expectsAudio && !metadata.has_audio) throw new MediaValidationError('rendered output is missing expected audio');
  const expectedSeconds = expected.frames / expected.fps;
  const tolerance = (1 / expected.fps) + AUDIO_TAIL_TOLERANCE_SECONDS;
  if (Math.abs(metadata.duration_seconds - expectedSeconds) > tolerance) throw new MediaValidationError('rendered output duration is outside one frame plus audio-tail tolerance');
};
