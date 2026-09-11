import path from 'node:path';
import express, { Request, Response } from 'express';
import { OutputCleanupError, RenderManager, RenderRequest } from './render.js';
import { validateMonthlyEditPlan } from './edit-plan.js';
import { loadMonthBank } from './month-bank.js';
import { prepareSceneV2, type PreparedSceneV2 } from './scene-v2.js';
import { resolvePrivateNarrationRetention } from './narration-archive.js';
import { parsePositiveInteger } from '../scripts/runtime-config.mjs';

const app = express();
app.use(express.json({ limit: '1mb' }));

const PORT = Number(process.env.PORT ?? 3001);
const HOST = process.env.HOST;
const RENDER_JOB_CONCURRENCY = parsePositiveInteger(
  process.env.REMOTION_JOB_CONCURRENCY ?? process.env.CONCURRENCY ?? '1',
  'REMOTION_JOB_CONCURRENCY',
);
const OUTPUT_DIR = path.join(process.cwd(), 'output');
const PUBLIC_BASE_URL = process.env.PUBLIC_BASE_URL ?? `http://localhost:${PORT}`;
const WEBHOOK_SECRET = process.env.WEBHOOK_SECRET;
const API_SECRET = process.env.REMOTION_API_SECRET;
const ALLOW_COMPOSITION_CODE = process.env.REMOTION_ALLOW_COMPOSITION_CODE === 'true';
const SANDBOX_ENABLED = process.env.REMOTION_SANDBOX_ENABLED === 'true';
const SANDBOX_STRICT = process.env.REMOTION_SANDBOX_STRICT === 'true';
const OUTPUT_PUBLIC = process.env.REMOTION_OUTPUT_PUBLIC === 'true';
const MAX_JOBS = Number(process.env.REMOTION_MAX_JOBS ?? 1000);
const JOB_TTL_SECONDS = Number(process.env.REMOTION_JOB_TTL_SECONDS ?? 6 * 60 * 60);
const OUTPUT_TTL_SECONDS = Number(process.env.REMOTION_OUTPUT_TTL_SECONDS ?? 24 * 60 * 60);
const RENDER_TIMEOUT_SECONDS = Number(process.env.REMOTION_RENDER_TIMEOUT_SECONDS ?? 900);
const CALLBACK_TIMEOUT_MS = Number(process.env.REMOTION_CALLBACK_TIMEOUT_MS ?? 5000);
const CALLBACK_ALLOWLIST = (process.env.REMOTION_CALLBACK_ALLOWLIST ?? '')
  .split(',')
  .map((v) => v.trim())
  .filter(Boolean);

const isExplicitLoopbackHost = (host: string | undefined): boolean =>
  host === 'localhost' || host === '127.0.0.1' || host === '::1';

const requireStartupSecurity = (): void => {
  if (process.env.NODE_ENV === 'production') {
    if (!API_SECRET) throw new Error('REMOTION_API_SECRET is required in production');
    if (!WEBHOOK_SECRET) throw new Error('WEBHOOK_SECRET is required in production');
    return;
  }
  if (!API_SECRET && !isExplicitLoopbackHost(HOST)) {
    throw new Error('HOST must be an explicit loopback address when REMOTION_API_SECRET is unset');
  }
};

requireStartupSecurity();

const PRESERVE_NARRATION = resolvePrivateNarrationRetention({
  preserveNarration: process.env.REMOTION_PRESERVE_NARRATION === 'true',
  nodeEnv: process.env.NODE_ENV,
  host: HOST,
});

const manager = new RenderManager({
  concurrency: RENDER_JOB_CONCURRENCY,
  outputDir: OUTPUT_DIR,
  publicBaseUrl: PUBLIC_BASE_URL,
  webhookSecret: WEBHOOK_SECRET,
  maxJobs: MAX_JOBS,
  jobTtlSeconds: JOB_TTL_SECONDS,
  outputTtlSeconds: OUTPUT_TTL_SECONDS,
  renderTimeoutMs: Math.max(10_000, RENDER_TIMEOUT_SECONDS * 1000),
  callbackAllowlist: CALLBACK_ALLOWLIST,
  callbackTimeoutMs: Math.max(1000, CALLBACK_TIMEOUT_MS),
  preserveNarration: PRESERVE_NARRATION,
});

const isAuthValid = (req: Request) => {
  if (!API_SECRET) return true;
  const headerAuth = req.headers.authorization;
  const apiKey = req.headers['x-api-key'];
  return headerAuth === `Bearer ${API_SECRET}` || apiKey === API_SECRET;
};

// Simple auth layer: require shared secret when configured
app.use((req, res, next) => {
  if (!API_SECRET) {
    return next();
  }
  if (req.path === '/health') {
    return next();
  }
  if (OUTPUT_PUBLIC && req.path.startsWith('/output')) {
    return next();
  }
  if (isAuthValid(req)) {
    return next();
  }
  res.status(401).json({ error: 'unauthorized' });
});

app.use('/output', express.static(OUTPUT_DIR));

app.get('/health', (_req, res) => {
  res.json({ status: 'ok', ...manager.getHealth() });
});

/** Read-only readiness check used before title/audio generation is authorized. */
app.post('/api/render/presets/preflight', async (req: Request, res: Response) => {
  try {
    const body = req.body ?? {};
    await loadMonthBank({
      presetId: body.render_preset?.id,
      presetSha256: body.render_preset?.sha256,
      voiceProfileDigest: body.voice_profile_digest,
      voiceProfile: body.voice_profile,
    });
    res.json({ status: 'ready' });
  } catch (error) {
    res.status(409).json({ error: error instanceof Error ? error.message : 'scene-v2 preset unavailable' });
  }
});

const isAllowedCallbackUrl = (url: string): boolean => {
  if (!url) return false;
  try {
    const parsed = new URL(url);
    const protocol = parsed.protocol.toLowerCase();
    const hostname = parsed.hostname.toLowerCase();

    if (protocol === 'http:') {
      return CALLBACK_ALLOWLIST.some((allowed) => {
        try {
          const configured = new URL(allowed);
          return configured.protocol === 'http:' && configured.origin === parsed.origin;
        } catch {
          return false;
        }
      });
    }
    if (protocol !== 'https:') return false;

    if (CALLBACK_ALLOWLIST.length === 0) {
      return hostname === 'localhost' || hostname === '127.0.0.1';
    }

    return CALLBACK_ALLOWLIST.some((allowed) => {
      if (allowed.includes('://')) return false;
      const dom = allowed.toLowerCase().replace(/^\.+|\.+$/g, '');
      if (!dom) return false;
      return hostname === dom || hostname.endsWith(`.${dom}`);
    });
  } catch {
    return false;
  }
};

app.post('/api/render', async (req: Request, res: Response) => {
  const body: Partial<RenderRequest> = req.body ?? {};

  // Must have either template or composition_code
  const hasTemplate = body.template && typeof body.template === 'string';
  const hasCode = body.composition_code && typeof body.composition_code === 'string';

  if (!hasTemplate && !hasCode) {
    res.status(400).json({ error: 'Either template or composition_code is required' });
    return;
  }

  // Require sandbox for custom code to reduce supply-chain risk
  if (hasCode && (!SANDBOX_ENABLED || !SANDBOX_STRICT)) {
    res.status(400).json({ error: 'composition_code is disabled without strict sandboxing; use template mode' });
    return;
  }

  if (hasCode && !ALLOW_COMPOSITION_CODE) {
    res.status(400).json({ error: 'composition_code not allowed by configuration' });
    return;
  }

  // Scene-v2 intentionally carries media only inside ordered_scenes. It is
  // expanded after preset/profile validation and before the monthly branch.
  if (body.template !== 'monthly-scene-v2' && (!Array.isArray(body.clips) || body.clips.length === 0)) {
    res.status(400).json({ error: 'clips must be a non-empty array' });
    return;
  }

  // Support objects/texts at top level OR inside input_props (for tool compatibility)
  const inputProps = (body as any).input_props ?? {};
  let objects = body.objects ?? inputProps.objects;
  const texts = body.texts ?? inputProps.texts;
  let narrationUrls = body.narration_urls ?? inputProps.narration_urls;
  const musicUrl = body.music_url ?? inputProps.music_url;
  const editPlan = body.edit_plan ?? inputProps.edit_plan;

  let sceneV2: PreparedSceneV2 | undefined;
  if (body.template === 'monthly-scene-v2') {
    try {
      sceneV2 = await prepareSceneV2(body as Record<string, unknown>);
    } catch (error) {
      res.status(409).json({ error: error instanceof Error ? error.message : 'scene-v2 preset unavailable' });
      return;
    }
    objects = body.objects;
    narrationUrls = body.narration_urls;
  }
  if (body.template === 'monthly') {
    try { validateMonthlyEditPlan(editPlan); } catch (error) { res.status(400).json({ error: error instanceof Error ? error.message : 'invalid monthly edit_plan' }); return; }
    if (!Array.isArray(body.clips) || body.clips.length !== 12) {
      res.status(400).json({ error: 'monthly requires exactly 12 clip URLs' });
      return;
    }
    if (!Array.isArray(objects) || objects.length !== 12 || objects.some((item) => typeof item !== 'string' || !item.trim())) {
      res.status(400).json({ error: 'monthly requires exactly 12 non-empty object labels' });
      return;
    }
    if (narrationUrls !== undefined && (!Array.isArray(narrationUrls) || narrationUrls.length !== 12 || narrationUrls.some((item) => typeof item !== 'string' || !item.trim()))) {
      res.status(400).json({ error: 'monthly narration_urls must contain exactly 12 URLs when supplied' });
      return;
    }
    if (musicUrl !== undefined && (typeof musicUrl !== 'string' || !musicUrl.trim())) {
      res.status(400).json({ error: 'monthly music_url must be a non-empty URL when supplied' });
      return;
    }
    const width = body.width ?? 1080;
    const height = body.height ?? 1920;
    if (!Number.isInteger(width) || !Number.isInteger(height) || width <= 0 || height <= 0 || width * 16 !== height * 9) {
      res.status(400).json({ error: 'monthly requires positive 9:16 portrait dimensions' });
      return;
    }
  }

  // Support both duration_frames and duration_seconds
  const fps = body.fps ?? 30;
  if (hasTemplate && fps !== 30) {
    res.status(400).json({ error: 'template renders require fps=30 to match fixed timelines' });
    return;
  }
  let durationFrames = body.duration_frames;
  if (!durationFrames && (body as any).duration_seconds) {
    durationFrames = Math.round((body as any).duration_seconds * fps);
  }
  durationFrames = durationFrames ?? 300;

  const request: RenderRequest = {
    run_id: body.run_id,
    input_hash: body.input_hash,
    template: body.template,
    composition_code: body.composition_code,
    clips: body.clips,
    objects: objects,
    texts: texts,
    narration_urls: narrationUrls,
    music_url: musicUrl,
    edit_plan: body.template === "monthly" ? validateMonthlyEditPlan(editPlan) : undefined,
    duration_frames: durationFrames,
    fps: fps,
    width: body.width ?? 1080,
    height: body.height ?? 1920,
    callback_url: body.callback_url,
    scene_v2: sceneV2,
  };

  if (request.callback_url && !isAllowedCallbackUrl(request.callback_url)) {
    res.status(400).json({ error: 'callback_url is not allowlisted' });
    return;
  }

  try {
    const job = await manager.enqueue(request);
    res.status(202).json({
      job_id: job.id,
      status: job.status,
      template: job.template,
      run_id: job.runId,
      input_hash: job.inputHash,
      media_diagnostics: job.mediaDiagnostics,
      render_status: job.renderStatus,
    });
  } catch (error) {
    console.error('Failed to enqueue render', error);
    const message = error instanceof Error ? error.message : String(error);
    if (message.includes('queue is full')) {
      res.status(429).json({ error: 'render_queue_full' });
      return;
    }
    res.status(500).json({ error: 'Failed to queue render' });
  }
});

app.get('/api/render/:jobId', (req: Request, res: Response) => {
  const job = manager.getJob(req.params.jobId);
  if (!job) {
    res.status(404).json({ error: 'job not found' });
    return;
  }

  res.json({
    status: job.status,
    progress: job.progress,
    output_url: job.outputUrl,
    template: job.template,
    run_id: job.runId,
    input_hash: job.inputHash,
    video_metadata: job.videoMetadata,
    media_diagnostics: job.mediaDiagnostics,
    render_status: job.renderStatus,
    error: job.error,
  });
});

app.delete('/api/render/:jobId/output', async (req: Request, res: Response) => {
  const body = req.body ?? {};
  const runId = body.run_id;
  const inputHash = body.input_hash;
  const expectedSha256 = body.expected_sha256;
  if (
    typeof runId !== 'string' || !runId
    || typeof inputHash !== 'string' || !inputHash
    || typeof expectedSha256 !== 'string' || !/^[a-f0-9]{64}$/.test(expectedSha256)
  ) {
    res.status(400).json({ error: 'invalid_output_cleanup_request' });
    return;
  }
  try {
    const status = await manager.deleteVerifiedOutput({
      id: req.params.jobId,
      runId,
      inputHash,
      expectedSha256,
    });
    res.json({ status });
  } catch (error) {
    if (error instanceof OutputCleanupError) {
      if (error.code === 'job_not_found') {
        res.status(404).json({ error: error.code });
        return;
      }
      res.status(409).json({ error: error.code });
      return;
    }
    console.error('Failed to remove verified render output', error);
    res.status(500).json({ error: 'output_cleanup_failed' });
  }
});

app.listen(PORT, HOST, () => {
  console.log(`Remotion render service listening on port ${PORT}, job concurrency=${RENDER_JOB_CONCURRENCY}`);
});
