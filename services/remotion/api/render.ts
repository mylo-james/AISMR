import path from 'node:path';
import fs from 'node:fs/promises';
import { createReadStream } from 'node:fs';
import crypto from 'node:crypto';
import PQueue from 'p-queue';
import { nanoid } from 'nanoid';
import { MonthlyEditPlan, resolveMonthlyTiming } from './edit-plan.js';
import { MediaMetadata, stageMedia, probeOutputFile, verifyOutputDecode, framesForDuration, assertRenderedOutput } from './media.js';
import { assembleMonthTitleWav, loadMonthBank, type SceneV2 } from './month-bank.js';
import { preserveSceneNarration, type NarrationArchiveReceipt } from './narration-archive.js';

// Dynamic import for ESM module
const renderVideoModule = import('../render.mjs');
const MAX_JOB_STAGED_BYTES = Math.max(1_024, Number(process.env.REMOTION_MAX_JOB_STAGED_BYTES ?? 256 * 1024 * 1024));

export type JobStatus = 'queued' | 'rendering' | 'done' | 'error';
export const RENDER_STATUS_PHASES = [
  'queued', 'preparing', 'composition', 'frames', 'encoding', 'verification', 'complete', 'failed',
] as const;
export type RenderStatusPhase = typeof RENDER_STATUS_PHASES[number];

export interface RenderStatus {
  schema_version: 1;
  phase: RenderStatusPhase;
  progress: number;
  total_frames?: number;
  rendered_frames?: number;
  encoded_frames?: number;
  stitch_stage?: 'encoding' | 'muxing';
}

export interface RenderRequest {
  run_id?: string;
  input_hash?: string;
  template?: string;           // Template name (e.g., "aismr", "motivational")
  composition_code?: string;   // Custom TSX code (if no template)
  clips: string[];
  objects?: string[];          // Object names for AISMR template
  texts?: string[];            // Text overlays for motivational template
  narration_urls?: string[];
  music_url?: string;
  edit_plan?: MonthlyEditPlan;
  duration_frames: number;
  fps: number;
  width: number;
  height: number;
  callback_url?: string;
  scene_v2?: {
    scenes: SceneV2[];
    preset: { id: string; sha256: string };
    voice_profile_digest: string;
    voice_profile: Record<string, unknown>;
  };
}

export interface MediaDiagnostics {
  input?: { clips: MediaMetadata[]; narration?: MediaMetadata[]; music?: MediaMetadata };
  output?: MediaMetadata;
  verified: boolean;
  errors: string[];
  provenance?: Record<string, unknown>;
  storage_error?: { code: string; details: Record<string, unknown> };
}

export interface RenderJob {
  id: string;
  runId?: string;
  inputHash?: string;
  template?: string;
  status: JobStatus;
  progress: number;
  renderStatus: RenderStatus;
  outputPath?: string;
  outputUrl?: string;
  error?: string | null;
  callbackUrl?: string;
  videoMetadata?: MediaMetadata;
  mediaDiagnostics: MediaDiagnostics;
  createdAt: number;
  deletedOutput?: { runId: string; inputHash: string; sha256: string };
}

export const storageErrorDiagnostics = (error: unknown): MediaDiagnostics['storage_error'] | undefined => {
  if (error === null || (typeof error !== 'object' && typeof error !== 'function')) return undefined;
  const storageError = error as { code?: unknown; details?: unknown };
  if (storageError.code !== 'render_storage_budget_exceeded'
    || storageError.details === null
    || typeof storageError.details !== 'object'
    || Array.isArray(storageError.details)) return undefined;
  return {
    code: storageError.code,
    details: { ...storageError.details as Record<string, unknown> },
  };
};

export class OutputCleanupError extends Error {
  constructor(public readonly code: 'job_not_found' | 'output_not_eligible' | 'output_hash_mismatch' | 'output_missing') {
    super(code);
  }
}

const sha256File = async (filePath: string): Promise<string> => new Promise((resolve, reject) => {
  const hash = crypto.createHash('sha256');
  const stream = createReadStream(filePath);
  stream.on('error', reject);
  stream.on('data', (chunk) => hash.update(chunk));
  stream.on('end', () => resolve(hash.digest('hex')));
});

interface RenderManagerInit {
  concurrency: number;
  outputDir: string;
  publicBaseUrl: string;
  webhookSecret?: string;
  maxJobs?: number;
  jobTtlSeconds?: number;
  outputTtlSeconds?: number;
  renderTimeoutMs?: number;
  callbackAllowlist?: string[];
  callbackTimeoutMs?: number;
  preserveNarration?: boolean;
}

export class RenderManager {
  private queue: PQueue;
  private jobs: Map<string, RenderJob> = new Map();
  private outputCleanupChains: Map<string, Promise<void>> = new Map();
  private outputDir: string;
  private publicBaseUrl: string;
  private webhookSecret?: string;
  private maxJobs: number;
  private jobTtlMs: number;
  private outputTtlMs: number;
  private renderTimeoutMs: number;
  private callbackAllowlist: string[];
  private callbackTimeoutMs: number;
  private preserveNarration: boolean;

  constructor({
    concurrency,
    outputDir,
    publicBaseUrl,
    webhookSecret,
    maxJobs = 1000,
    jobTtlSeconds = 6 * 60 * 60,
    outputTtlSeconds = 24 * 60 * 60,
    renderTimeoutMs = 15 * 60 * 1000,
    callbackAllowlist = [],
    callbackTimeoutMs = 5000,
    preserveNarration = false,
  }: RenderManagerInit) {
    this.queue = new PQueue({ concurrency: Math.max(1, concurrency) });
    this.outputDir = path.resolve(outputDir);
    this.publicBaseUrl = publicBaseUrl.replace(/\/$/, '');
    this.webhookSecret = webhookSecret;
    this.maxJobs = Math.max(100, Number.isFinite(maxJobs) ? maxJobs : 1000);
    const jobTtlMs = Number.isFinite(jobTtlSeconds) ? jobTtlSeconds * 1000 : 6 * 60 * 60 * 1000;
    const outputTtlMs = Number.isFinite(outputTtlSeconds)
      ? outputTtlSeconds * 1000
      : 24 * 60 * 60 * 1000;
    this.jobTtlMs = Math.max(60_000, jobTtlMs);
    this.outputTtlMs = Math.max(0, outputTtlMs);
    this.renderTimeoutMs = Math.max(10_000, Number(renderTimeoutMs) || 0);
    this.callbackAllowlist = callbackAllowlist.map((v) => v.toLowerCase());
    this.callbackTimeoutMs = Math.max(1000, Number(callbackTimeoutMs) || 5000);
    this.preserveNarration = preserveNarration;
  }

  public async enqueue(request: RenderRequest): Promise<RenderJob> {
    if (this.jobs.size >= this.maxJobs) {
      throw new Error('Render queue is full');
    }
    const id = nanoid();
    const job: RenderJob = {
      id,
      runId: request.run_id,
      inputHash: request.input_hash,
      template: request.template,
      status: 'queued',
      progress: 0,
      renderStatus: { schema_version: 1, phase: 'queued', progress: 0 },
      error: null,
      callbackUrl: request.callback_url,
      mediaDiagnostics: { verified: false, errors: [] },
      createdAt: Date.now(),
    };

    this.jobs.set(id, job);
    await fs.mkdir(this.outputDir, { recursive: true });

    void this.pruneStale();
    void this.queue.add(() => this.processJob(job, request));
    return job;
  }

  public getJob(id: string): RenderJob | undefined {
    return this.jobs.get(id);
  }

  /**
   * Delete a completed output only after the caller proves it has accepted the
   * identical bytes. This remains intentionally scoped to a live in-memory
   * receipt: after a renderer restart, normal output TTL cleanup is the safe
   * fallback rather than reconstructing authority from a filename.
   */
  public async deleteVerifiedOutput({
    id,
    runId,
    inputHash,
    expectedSha256,
  }: {
    id: string;
    runId: string;
    inputHash: string;
    expectedSha256: string;
  }): Promise<'deleted' | 'already_deleted'> {
    return this.serializeOutputCleanup(id, async () => this.deleteVerifiedOutputUnlocked({
      id,
      runId,
      inputHash,
      expectedSha256,
    }));
  }

  private async deleteVerifiedOutputUnlocked({
    id,
    runId,
    inputHash,
    expectedSha256,
  }: {
    id: string;
    runId: string;
    inputHash: string;
    expectedSha256: string;
  }): Promise<'deleted' | 'already_deleted'> {
    const job = this.jobs.get(id);
    if (!job) throw new OutputCleanupError('job_not_found');
    if (
      job.status !== 'done'
      || job.mediaDiagnostics.verified !== true
      || job.runId !== runId
      || job.inputHash !== inputHash
    ) {
      throw new OutputCleanupError('output_not_eligible');
    }
    const outputPath = this.ownedOutputPath(id, job.outputPath);
    const prior = job.deletedOutput;
    if (prior) {
      if (
        prior.runId === runId
        && prior.inputHash === inputHash
        && prior.sha256 === expectedSha256
      ) {
        return 'already_deleted';
      }
      throw new OutputCleanupError('output_not_eligible');
    }
    try {
      const actualSha256 = await sha256File(outputPath);
      if (actualSha256 !== expectedSha256) {
        throw new OutputCleanupError('output_hash_mismatch');
      }
      await fs.unlink(outputPath);
      job.deletedOutput = { runId, inputHash, sha256: actualSha256 };
      return 'deleted';
    } catch (error) {
      if (error instanceof OutputCleanupError) throw error;
      if ((error as NodeJS.ErrnoException).code === 'ENOENT') {
        throw new OutputCleanupError('output_missing');
      }
      throw error;
    }
  }

  private async serializeOutputCleanup<T>(id: string, operation: () => Promise<T>): Promise<T> {
    const prior = this.outputCleanupChains.get(id) ?? Promise.resolve();
    let release: () => void;
    const current = new Promise<void>((resolve) => {
      release = resolve;
    });
    const chain = prior.catch(() => undefined).then(() => current);
    this.outputCleanupChains.set(id, chain);
    await prior.catch(() => undefined);
    try {
      return await operation();
    } finally {
      release!();
      if (this.outputCleanupChains.get(id) === chain) {
        this.outputCleanupChains.delete(id);
      }
    }
  }

  public getHealth() {
    return {
      queued: this.queue.size,
      pending: this.queue.pending,
      totalJobs: this.jobs.size,
    };
  }

  private pruneJobs(now: number) {
    const cutoff = now - this.jobTtlMs;
    for (const [id, job] of this.jobs.entries()) {
      if ((job.status === 'done' || job.status === 'error') && job.createdAt < cutoff) {
        this.jobs.delete(id);
      }
    }

    if (this.jobs.size <= this.maxJobs) return;

    const candidates = Array.from(this.jobs.values())
      .filter((job) => job.status === 'done' || job.status === 'error')
      .sort((a, b) => a.createdAt - b.createdAt);

    for (const job of candidates) {
      this.jobs.delete(job.id);
      if (this.jobs.size <= this.maxJobs) return;
    }
  }

  private async pruneOutputs(now: number) {
    if (this.outputTtlMs <= 0) return;
    try {
      const entries = await fs.readdir(this.outputDir);
      await Promise.all(entries.map(async (name) => {
        if (!name.endsWith('.mp4')) return;
        const filePath = path.join(this.outputDir, name);
        try {
          const stat = await fs.stat(filePath);
          if (now - stat.mtimeMs > this.outputTtlMs) {
            await fs.unlink(filePath);
          }
        } catch {
          return;
        }
      }));
    } catch {
      return;
    }
  }

  private async pruneStale() {
    const now = Date.now();
    this.pruneJobs(now);
    await this.pruneOutputs(now);
  }

  private ownedOutputPath(id: string, outputPath: string | undefined): string {
    const expected = path.resolve(this.outputDir, `${id}.mp4`);
    if (!outputPath || path.resolve(outputPath) !== expected) {
      throw new OutputCleanupError('output_not_eligible');
    }
    return expected;
  }

  private async processJob(job: RenderJob, request: RenderRequest) {
    job.status = 'rendering';
    job.renderStatus = { schema_version: 1, phase: 'preparing', progress: 0 };
    const outputPath = path.join(this.outputDir, `${job.id}.mp4`);
    const stagingDir = path.join(this.outputDir, '.staging', job.id);

    try {
      const { renderVideo } = await renderVideoModule;

      let segmentFrames: number[] | undefined;
      let narrationFrames: number[] | undefined;
      if (request.template === 'monthly') {
        let stagedBytes = 0;
        const stageWithBudget = async (url: string, basename: string) => {
          const asset = await stageMedia(url, stagingDir, basename);
          if (stagedBytes + asset.bytes > MAX_JOB_STAGED_BYTES) throw new Error('monthly staged media exceeds job size limit');
          stagedBytes += asset.bytes;
          return asset;
        };
        const stagedClips = [];
        for (let index = 0; index < request.clips.length; index += 1) {
          try {
            stagedClips.push(await stageWithBudget(request.clips[index], `clip-${index}`));
          } catch (error) {
            throw new Error(`clip ${index + 1} preflight failed: ${error instanceof Error ? error.message : String(error)}`);
          }
        }
        const clips = stagedClips.map((item) => item.metadata);
        if (clips.some((clip) => !clip.has_video)) throw new Error('monthly clips must each contain a video stream');
        segmentFrames = clips.map((clip) => framesForDuration(clip.duration_seconds, request.fps));
        const stagedNarration = [];
        if (request.narration_urls) {
          for (let index = 0; index < request.narration_urls.length; index += 1) {
            try {
              stagedNarration.push(await stageWithBudget(request.narration_urls[index], `narration-${index}`));
            } catch (error) {
              throw new Error(`narration ${index + 1} preflight failed: ${error instanceof Error ? error.message : String(error)}`);
            }
          }
        }
        const narration = stagedNarration.map((item) => item.metadata);
        if (narration.some((item) => !item.has_audio)) throw new Error('monthly narration must each contain an audio stream');
        let assembledNarration = stagedNarration;
        if (request.scene_v2) {
          const bank = await loadMonthBank({
            presetId: request.scene_v2.preset.id,
            presetSha256: request.scene_v2.preset.sha256,
            voiceProfileDigest: request.scene_v2.voice_profile_digest,
            voiceProfile: request.scene_v2.voice_profile,
          });
          // A scene-v2 preset owns timeline placement. Caller edit controls may
          // tune bounded visual/mix values but cannot move narration or change
          // the reviewed 202-frame segment duration.
          request.edit_plan = {
            ...request.edit_plan,
            segment_frames: Array(12).fill(bank.preset.segment_frames),
            narration_start_frames: Array(12).fill(bank.preset.narration_start_frames),
          };
          assembledNarration = [];
          for (let index = 0; index < stagedNarration.length; index += 1) {
            const assembled = await assembleMonthTitleWav({ bank, index, titleAudioPath: stagedNarration[index].localPath, directory: stagingDir });
            if (stagedBytes + assembled.bytes > MAX_JOB_STAGED_BYTES) throw new Error('monthly assembled media exceeds job size limit');
            stagedBytes += assembled.bytes;
            if (!assembled.metadata.has_audio || assembled.metadata.sample_rate !== 44100) throw new Error(`assembled narration ${index + 1} is invalid`);
            assembledNarration.push(assembled);
          }
          const assembledWavSha256 = await Promise.all(assembledNarration.map((asset) => sha256File(asset.localPath)));
          const narrationProvenance = {
            render_contract: 'scene-v2',
            render_preset_id: bank.preset.id,
            render_preset_sha256: request.scene_v2.preset.sha256,
            month_bank_id: bank.manifest.bank_id,
            month_bank_sha256: bank.manifestSha256,
            voice_profile_sha256: request.scene_v2.voice_profile_digest,
            join_pause_seconds: bank.preset.join_pause_seconds,
            voice_profile: request.scene_v2.voice_profile,
            month_bank_source: (bank.manifest as unknown as { source?: unknown; rights?: unknown }).source,
            month_bank_rights: (bank.manifest as unknown as { rights?: unknown }).rights,
            assembled_wav_sha256: assembledWavSha256,
            title_audio_sha256: await Promise.all(stagedNarration.map((asset) => sha256File(asset.localPath))),
            video_sha256: await Promise.all(stagedClips.map((asset) => sha256File(asset.localPath))),
            bank_clip_refs: bank.manifest.clips.map((clip) => ({ ordinal: clip.ordinal, label: clip.label, filename: clip.filename, sha256: clip.sha256 })),
          };
          let narrationArchive: NarrationArchiveReceipt | undefined;
          if (this.preserveNarration) {
            narrationArchive = await preserveSceneNarration({
              enabled: true,
              outputDir: this.outputDir,
              stagingDir,
              jobId: job.id,
              provenance: {
                render_preset_id: bank.preset.id,
                render_preset_sha256: request.scene_v2.preset.sha256,
                voice_profile_sha256: request.scene_v2.voice_profile_digest,
                month_bank_id: bank.manifest.bank_id,
                month_bank_sha256: bank.manifestSha256,
                join_pause_seconds: bank.preset.join_pause_seconds,
              },
              assembledWavs: assembledNarration.map((asset, index) => ({ ordinal: index + 1, path: asset.localPath })),
            });
          }
          job.mediaDiagnostics.provenance = {
            ...narrationProvenance,
            ...(narrationArchive ? { narration_archive: narrationArchive } : {}),
          };
        }
        const finalNarration = assembledNarration.map((item) => item.metadata);
        const timing = resolveMonthlyTiming(
          request.edit_plan,
          clips.map((clip) => clip.duration_seconds),
          finalNarration.map((item) => item.duration_seconds),
          request.fps,
        );
        segmentFrames = timing.segmentFrames;
        narrationFrames = timing.narrationFrames;
        request.edit_plan = {
          ...request.edit_plan,
          clip_playback_rates: timing.clipPlaybackRates,
          narration_start_frames: timing.narrationStartFrames,
          ...(timing.fadeFrames === undefined ? {} : { fade_frames: timing.fadeFrames }),
          ...(timing.sceneEffects === undefined ? {} : { scene_effects: timing.sceneEffects }),
        };
        let stagedMusic;
        try {
          stagedMusic = request.music_url ? await stageWithBudget(request.music_url, 'music') : undefined;
        } catch (error) {
          throw new Error(`music preflight failed: ${error instanceof Error ? error.message : String(error)}`);
        }
        const music = stagedMusic?.metadata;
        if (music && !music.has_audio) throw new Error('monthly music must contain an audio stream');
        request.duration_frames = segmentFrames.reduce((total, frames) => total + frames, 0);
        job.mediaDiagnostics.input = { clips, ...(finalNarration.length ? { narration: finalNarration } : {}), ...(music ? { music } : {}) };
        request.clips = stagedClips.map((item) => item.filename);
        request.narration_urls = assembledNarration.length ? assembledNarration.map((item) => item.filename) : undefined;
        request.music_url = stagedMusic?.filename;
      }

      await renderVideo({
        jobId: job.id,
        template: request.template,
        compositionCode: request.composition_code,
        clips: request.clips,
        objects: request.objects,
        texts: request.texts,
        narrationUrls: request.narration_urls,
        musicUrl: request.music_url,
        segmentFrames,
        narrationFrames,
        clipPlaybackRates: request.edit_plan?.clip_playback_rates,
        fadeFrames: request.edit_plan?.fade_frames,
        narrationStartFrames: request.edit_plan?.narration_start_frames,
        sceneEffects: request.edit_plan?.scene_effects,
        narrationVolume: request.edit_plan?.narration_volume,
        musicVolume: request.edit_plan?.music_volume,
        musicDuckedVolume: request.edit_plan?.music_ducked_volume,
        publicDir: request.template === 'monthly' ? stagingDir : undefined,
        durationFrames: request.duration_frames,
        fps: request.fps,
        width: request.width,
        height: request.height,
        outputPath,
        timeoutMs: this.renderTimeoutMs,
        onProgress: (progress: number) => {
          job.progress = progress;
        },
        onStatus: (status: Omit<RenderStatus, 'schema_version'>) => {
          job.renderStatus = { schema_version: 1, ...status };
          job.progress = status.progress;
        },
      });

      job.renderStatus = { ...job.renderStatus, phase: 'verification' };
      const output = await probeOutputFile(outputPath);
      assertRenderedOutput(output, { width: request.width, height: request.height, fps: request.fps, frames: request.duration_frames, expectsAudio: Boolean(request.narration_urls?.length || request.music_url) });
      await verifyOutputDecode(outputPath);
      job.videoMetadata = output;
      job.mediaDiagnostics.output = output;
      job.mediaDiagnostics.verified = true;
      job.outputPath = outputPath;
      job.outputUrl = `${this.publicBaseUrl}/output/${path.basename(outputPath)}`;
      job.progress = 1;
      job.renderStatus = { ...job.renderStatus, phase: 'complete', progress: 1 };
      job.status = 'done';

      if (job.callbackUrl) {
        await this.sendCallback(job);
      }
    } catch (error) {
      console.error('Render failed', error);
      const observedProgress = job.renderStatus.progress;
      job.status = 'error';
      job.error = error instanceof Error ? error.message : String(error);
      const storageDiagnostics = storageErrorDiagnostics(error);
      if (storageDiagnostics) job.mediaDiagnostics.storage_error = storageDiagnostics;
      job.mediaDiagnostics.errors.push(job.error);
      job.progress = 0;
      job.renderStatus = { ...job.renderStatus, phase: 'failed', progress: observedProgress };

      if (job.callbackUrl) {
        await this.sendCallback(job);
      }
    } finally {
      await fs.rm(stagingDir, { recursive: true, force: true });
      void this.pruneStale();
    }
  }

  private async sendCallback(job: RenderJob) {
    if (!job.callbackUrl) return;
    if (!this.isCallbackAllowed(job.callbackUrl)) {
      console.error('Callback URL not allowlisted, skipping', job.callbackUrl);
      return;
    }

    try {
      const body = JSON.stringify({
        job_id: job.id,
        run_id: job.runId,
        input_hash: job.inputHash,
        status: job.status,
        output_url: job.outputUrl,
        error: job.error,
        video_metadata: job.videoMetadata,
        media_diagnostics: job.mediaDiagnostics,
        render_status: job.renderStatus,
      });

      const signature =
        this.webhookSecret
          ? `sha512=${crypto.createHmac('sha512', this.webhookSecret).update(body).digest('hex')}`
          : undefined;

      const controller = new AbortController();
      const timeout = setTimeout(() => controller.abort(), this.callbackTimeoutMs);
      try {
        await fetch(job.callbackUrl, {
          method: 'POST',
          headers: {
            'content-type': 'application/json',
            ...(signature ? { 'x-remotion-signature': signature } : {}),
          },
          body,
          signal: controller.signal,
        });
      } finally {
        clearTimeout(timeout);
      }
    } catch (error) {
      console.error('Failed to send webhook', error);
    }
  }

  private isCallbackAllowed(rawUrl: string): boolean {
    try {
      const parsed = new URL(rawUrl);
      const protocol = parsed.protocol.toLowerCase();
      const hostname = parsed.hostname.toLowerCase();

      if (protocol === 'http:') {
        return this.callbackAllowlist.some((allowed) => {
          try {
            const configured = new URL(allowed);
            return configured.protocol === 'http:' && configured.origin === parsed.origin;
          } catch {
            return false;
          }
        });
      }
      if (protocol !== 'https:') return false;

      if (this.callbackAllowlist.length === 0) {
        return hostname === 'localhost' || hostname === '127.0.0.1';
      }

      return this.callbackAllowlist.some((allowed) => {
        if (allowed.includes('://')) return false;
        const dom = allowed.replace(/^\.+|\.+$/g, '');
        if (!dom) return false;
        return hostname === dom || hostname.endsWith(`.${dom}`);
      });
    } catch {
      return false;
    }
  }
}
