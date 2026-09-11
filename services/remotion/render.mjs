import path from 'node:path';
import crypto from 'node:crypto';
import { mkdir, writeFile, copyFile, readdir, readFile, rm } from 'node:fs/promises';
import { existsSync, statfsSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { bundle } from '@remotion/bundler';
import { makeCancelSignal, openBrowser, renderMedia, selectComposition } from '@remotion/renderer';
import { fileURLToPath } from 'node:url';
import { parseFrameConcurrency } from './scripts/frame-concurrency.mjs';

const __dirname = path.dirname(fileURLToPath(import.meta.url));

const FRAME_CONCURRENCY = parseFrameConcurrency(process.env.REMOTION_FRAME_CONCURRENCY ?? '1');
const BUNDLE_CACHE_MAX = Number(process.env.REMOTION_BUNDLE_CACHE_MAX ?? 32);
const BUNDLE_CACHE_TTL_MS = Number(process.env.REMOTION_BUNDLE_CACHE_TTL_SECONDS ?? 3600) * 1000;
const BROWSER_IDLE_TTL_MS = Number(process.env.REMOTION_BROWSER_IDLE_TTL_SECONDS ?? 300) * 1000;

const MEBIBYTE = 1024 ** 2;
const GIBIBYTE = 1024 ** 3;
// The recent full-size monthly attempt produced JPEG frames no larger than 176,567
// bytes. This is a heuristic, not a bound on future content. Retain a 64 MiB
// margin and floor the estimated working set at 512 MiB for the 2,424-frame case.
const OBSERVED_MAX_JPEG_FRAME_BYTES = 176_567;
const TEMP_FRAME_OVERHEAD_BYTES = 64 * MEBIBYTE;
export const REMOTION_STORAGE_RESERVE_BYTES = 2 * GIBIBYTE;
export const REMOTION_STORAGE_WRITE_HEADROOM_BYTES = 64 * MEBIBYTE;
export const REMOTION_MIN_TEMP_FRAME_BUDGET_BYTES = 512 * MEBIBYTE;

export const estimateTempFrameBudgetBytes = (durationFrames) => {
  if (!Number.isSafeInteger(durationFrames) || durationFrames <= 0) {
    throw new Error('durationFrames must be a positive safe integer for storage estimation');
  }
  return Math.max(
    REMOTION_MIN_TEMP_FRAME_BUDGET_BYTES,
    (durationFrames * OBSERVED_MAX_JPEG_FRAME_BYTES) + TEMP_FRAME_OVERHEAD_BYTES,
  );
};

export const availableTempBytes = ({ tempDirectory = tmpdir(), statfs = statfsSync } = {}) => {
  const stats = statfs(tempDirectory);
  return Number(stats.bavail) * Number(stats.bsize);
};

export class RenderStorageBudgetError extends Error {
  constructor({ phase, freeBytes, requiredBytes, reserveBytes, writeHeadroomBytes, tempBudgetBytes }) {
    super(`renderer temporary storage ${phase}: ${freeBytes} bytes free, requires ${requiredBytes} bytes`);
    this.name = 'RenderStorageBudgetError';
    this.code = 'render_storage_budget_exceeded';
    this.details = Object.freeze({
      phase,
      free_bytes: freeBytes,
      required_bytes: requiredBytes,
      reserve_bytes: reserveBytes,
      write_headroom_bytes: writeHeadroomBytes,
      temp_frame_budget_bytes: tempBudgetBytes,
    });
  }
}

export const createRenderStorageGuard = ({
  durationFrames,
  freeBytes = availableTempBytes,
  reserveBytes = REMOTION_STORAGE_RESERVE_BYTES,
  writeHeadroomBytes = REMOTION_STORAGE_WRITE_HEADROOM_BYTES,
  tempBudgetBytes = estimateTempFrameBudgetBytes(durationFrames),
} = {}) => {
  const renderStartRequiredBytes = reserveBytes + writeHeadroomBytes + tempBudgetBytes;
  const progressRequiredBytes = reserveBytes + writeHeadroomBytes;
  let failure;

  const check = (phase, requiredBytes) => {
    const freeBytesNow = freeBytes();
    if (!Number.isFinite(freeBytesNow) || freeBytesNow < requiredBytes) {
      failure = new RenderStorageBudgetError({
        phase,
        freeBytes: freeBytesNow,
        requiredBytes,
        reserveBytes,
        writeHeadroomBytes,
        tempBudgetBytes,
      });
    }
    return failure;
  };

  return Object.freeze({
    assertCanStart: () => {
      const error = check('preflight', renderStartRequiredBytes);
      if (error) throw error;
    },
    checkProgress: () => check('progress', progressRequiredBytes),
    get failure() {
      return failure;
    },
  });
};

export const monitorRenderStorageProgress = ({ guard, cancel, onProgress, progress }) => {
  const storageError = guard.checkProgress();
  if (storageError) cancel();
  if (onProgress) onProgress(progress);
  return storageError;
};

export const renderStatusFromNativeProgress = ({
  progress,
  renderedFrames,
  encodedFrames,
  stitchStage,
  totalFrames,
}) => ({
  // Remotion can encode already-rendered frames while Chromium is still
  // producing more. Keep the visible phase at frames until all source frames
  // are observed, while exposing both counters and stitch_stage verbatim.
  phase: renderedFrames < totalFrames ? 'frames' : 'encoding',
  progress,
  total_frames: totalFrames,
  rendered_frames: renderedFrames,
  encoded_frames: encodedFrames,
  stitch_stage: stitchStage,
});

const bundleCache = new Map();
const bundleInFlight = new Map();

let browserPromise;
let browserLastUsed = 0;
let browserIdleTimer;
let activeRenderCount = 0;

const getBrowser = async () => {
  if (!browserPromise) {
    browserPromise = openBrowser('chrome', {
      chromiumOptions: {
        enableMultiProcessOnLinux: true,
      },
    });
  }
  try {
    const browser = await browserPromise;
    browserLastUsed = Date.now();
    scheduleBrowserIdleClose();
    return browser;
  } catch (err) {
    browserPromise = undefined;
    throw err;
  }
};

const scheduleBrowserIdleClose = () => {
  if (browserIdleTimer) {
    clearTimeout(browserIdleTimer);
  }
  if (BROWSER_IDLE_TTL_MS <= 0) return;
  browserIdleTimer = setTimeout(async () => {
    if (!browserPromise) return;
    if (activeRenderCount > 0) {
      scheduleBrowserIdleClose();
      return;
    }
    const idleFor = Date.now() - browserLastUsed;
    if (idleFor < BROWSER_IDLE_TTL_MS) {
      scheduleBrowserIdleClose();
      return;
    }
    try {
      const browser = await browserPromise;
      await browser.close({silent: true});
    } catch {
      // ignore
    } finally {
      browserPromise = undefined;
    }
  }, BROWSER_IDLE_TTL_MS);
};

export const closeRenderer = async () => {
  if (activeRenderCount > 0) throw new Error('cannot close renderer while a render is active');
  if (browserIdleTimer) clearTimeout(browserIdleTimer);
  browserIdleTimer = undefined;
  if (!browserPromise) return;
  const pending = browserPromise;
  browserPromise = undefined;
  const browser = await pending;
  await browser.close({silent: true});
};

const sha256 = (value) => crypto.createHash('sha256').update(value).digest('hex').slice(0, 16);

const bundleKey = ({
  compositionHash,
  width,
  height,
  fps,
  durationFrames,
  assetNamespace,
}) => {
  const base = `comp:${compositionHash}`;
  return `${base}-w${width}-h${height}-fps${fps}-dur${durationFrames}-assets-${assetNamespace ?? 'shared'}`;
};

const ensureComponentsCopied = async (bundleDir) => {
  const componentsDir = path.join(bundleDir, 'components');
  if (existsSync(componentsDir)) return;
  await mkdir(componentsDir, { recursive: true });

  const srcComponentsDir = path.join(__dirname, 'src', 'components');
  const componentFiles = await readdir(srcComponentsDir);
  for (const file of componentFiles) {
    const srcPath = path.join(srcComponentsDir, file);
    const destPath = path.join(componentsDir, file);
    await copyFile(srcPath, destPath);
  }

  const indexContent = `export * from './VideoClip';
export * from './AnimatedText';
export * from './Transition';
export * from './Effects';
`;
  await writeFile(path.join(componentsDir, 'index.ts'), indexContent, 'utf8');
};

const buildCompositionSource = async ({ template, compositionCode }) => {
  let finalCompositionCode;

  if (template) {
    const templatePath = path.join(__dirname, 'templates', `${template}.tsx`);
    if (!existsSync(templatePath)) {
      throw new Error(`Template not found: ${template}`);
    }
    finalCompositionCode = await readFile(templatePath, 'utf8');
    finalCompositionCode = finalCompositionCode
      .split('\n')
      .filter(line => !line.trim().startsWith('import ') && !line.trim().startsWith('/**') && !line.trim().startsWith('*'))
      .join('\n');
  } else if (compositionCode) {
    finalCompositionCode = compositionCode
      .split('\n')
      .filter(line => !line.trim().startsWith('import '))
      .join('\n');
  } else {
    throw new Error('Either template or compositionCode must be provided');
  }

  const prelude = `import React from 'react';
import { AbsoluteFill, Sequence, Series, useCurrentFrame, useVideoConfig, interpolate, spring, Video, Audio, staticFile } from 'remotion';
import { OffthreadVideo } from 'remotion';
import { VideoClip, AnimatedText, Transition, ColorGrade, Vignette } from './components';
`;

  const postlude = `
// Detect exported composition component
const CompositionComponent =
  typeof MyVideo !== 'undefined' ? MyVideo :
  typeof RemotionComposition !== 'undefined' ? RemotionComposition :
  typeof Composition !== 'undefined' ? Composition :
  undefined;

if (!CompositionComponent) {
  throw new Error('No composition component exported. Export MyVideo, RemotionComposition, or Composition.');
}

export const DynamicComposition = CompositionComponent;
`;

  return `${prelude}\n${finalCompositionCode}\n${postlude}`;
};

const getCachedBundle = (key) => {
  const entry = bundleCache.get(key);
  if (!entry) return null;
  const now = Date.now();
  if (BUNDLE_CACHE_TTL_MS > 0 && now - entry.createdAt > BUNDLE_CACHE_TTL_MS) {
    bundleCache.delete(key);
    return null;
  }
  entry.lastUsed = now;
  return entry.bundle;
};

const pruneBundleCache = () => {
  if (BUNDLE_CACHE_TTL_MS > 0) {
    const now = Date.now();
    for (const [key, entry] of bundleCache.entries()) {
      if (now - entry.createdAt > BUNDLE_CACHE_TTL_MS) {
        bundleCache.delete(key);
      }
    }
  }
  if (BUNDLE_CACHE_MAX <= 0) {
    bundleCache.clear();
    return;
  }
  if (bundleCache.size <= BUNDLE_CACHE_MAX) return;
  const entries = Array.from(bundleCache.entries()).sort((a, b) => a[1].lastUsed - b[1].lastUsed);
  while (entries.length && bundleCache.size > BUNDLE_CACHE_MAX) {
    const [key] = entries.shift();
    bundleCache.delete(key);
  }
};

const ensureBundle = async ({ key, bundleDir, entryContents, compositionContents, publicDir }) => {
  pruneBundleCache();
  const cached = getCachedBundle(key);
  if (cached) return cached;
  if (bundleInFlight.has(key)) {
    return bundleInFlight.get(key);
  }
  const inFlight = (async () => {
    try {
      await mkdir(bundleDir, { recursive: true });
      await ensureComponentsCopied(bundleDir);
      const compositionFile = path.join(bundleDir, 'Composition.tsx');
      const entryFile = path.join(bundleDir, 'Entry.tsx');

      await writeFile(compositionFile, compositionContents, 'utf8');
      await writeFile(entryFile, entryContents, 'utf8');

      const bundled = await bundle({
        entryPoint: entryFile,
        ...(publicDir ? { publicDir } : {}),
        webpackOverride: (config) => config,
      });
      if (BUNDLE_CACHE_MAX > 0) {
        bundleCache.set(key, { bundle: bundled, createdAt: Date.now(), lastUsed: Date.now() });
      }
      return bundled;
    } finally {
      bundleInFlight.delete(key);
    }
  })();
  bundleInFlight.set(key, inFlight);
  return inFlight;
};

const SCENE_EFFECT_LIMITS = {
  zoomStart: [1, 1.18], zoomEnd: [1, 1.18],
  panXStart: [-0.03, 0.03], panXEnd: [-0.03, 0.03],
  panYStart: [-0.03, 0.03], panYEnd: [-0.03, 0.03],
  saturation: [0.8, 1.15], contrast: [0.9, 1.1], vignette: [0, 0.22],
};

export const validateSceneEffects = (sceneEffects, clipCount) => {
  if (sceneEffects === undefined) return undefined;
  if (!Array.isArray(sceneEffects) || sceneEffects.length !== clipCount) {
    throw new Error('sceneEffects must contain exactly one object per clip');
  }
  return sceneEffects.map((effect, index) => {
    if (!effect || typeof effect !== 'object' || Array.isArray(effect)) {
      throw new Error(`sceneEffects[${index}] must be an object`);
    }
    for (const [name, value] of Object.entries(effect)) {
      const range = SCENE_EFFECT_LIMITS[name];
      if (!Object.hasOwn(SCENE_EFFECT_LIMITS, name) || !range || typeof value !== 'number' || !Number.isFinite(value) || value < range[0] || value > range[1]) {
        throw new Error(`sceneEffects[${index}].${name} is invalid`);
      }
    }
    return { ...effect };
  });
};

const validateNumericArray = (name, values, clipCount, predicate) => {
  if (values === undefined) return undefined;
  if (!Array.isArray(values) || values.length !== clipCount || values.some((value) => typeof value !== 'number' || !Number.isFinite(value) || !predicate(value))) {
    throw new Error(`${name} must contain exactly one valid number per clip`);
  }
  return [...values];
};

/**
 * Render a video using either a template or custom composition code.
 *
 * @param {Object} options
 * @param {string} options.jobId - Unique job identifier
 * @param {string} [options.template] - Template name (e.g., "aismr", "motivational") - if provided, uses pre-built template
 * @param {string} [options.compositionCode] - Custom TSX code (used if no template)
 * @param {string[]} options.clips - Array of video URLs
 * @param {string[]} [options.objects] - Array of object names (for AISMR template)
 * @param {string[]} [options.texts] - Array of text overlays (for motivational template)
 * @param {string[]} [options.narrationUrls] - Narration URLs (for monthly template)
 * @param {string} [options.musicUrl] - Music URL (for monthly template)
 * @param {number[]} [options.segmentFrames] - Measured clip frames (for monthly template)
 * @param {number[]} [options.narrationFrames] - Measured narration frames (for monthly template)
 * @param {number[]} [options.clipPlaybackRates] - Optional playback rate per monthly clip
 * @param {number} [options.fadeFrames] - Optional per-segment fade length in frames
 * @param {number[]} [options.narrationStartFrames] - Optional narration offset per segment
 * @param {object[]} [options.sceneEffects] - Optional bounded per-scene editor effects
 * @param {number} [options.narrationVolume] - Narration gain from 0 through 1
 * @param {number} [options.musicVolume] - Music gain between narrations from 0 through 1
 * @param {number} [options.musicDuckedVolume] - Music gain under narration from 0 through 1
 * @param {string} [options.publicDir] - Per-job staged assets directory (for monthly template)
 * @param {number} options.durationFrames - Total duration in frames
 * @param {number} options.fps - Frames per second
 * @param {number} options.width - Output width
 * @param {number} options.height - Output height
 * @param {string} options.outputPath - Where to save the rendered video
 * @param {number} [options.timeoutMs] - Cancel render after this many milliseconds
 * @param {Function} [options.onProgress] - Progress callback
 * @param {Function} [options.onStatus] - Additive observed render-status callback
 * @param {Function} [options.storageGuardFactory] - Test seam for the temporary-storage guard
 */
export async function renderVideo({
  jobId,
  template,
  compositionCode,
  clips,
  objects,
  texts,
  narrationUrls,
  musicUrl,
  segmentFrames,
  narrationFrames,
  clipPlaybackRates,
  fadeFrames,
  narrationStartFrames,
  sceneEffects,
  narrationVolume,
  musicVolume,
  musicDuckedVolume,
  publicDir,
  durationFrames,
  fps,
  width,
  height,
  outputPath,
  timeoutMs,
  onProgress,
  onStatus,
  storageGuardFactory = (frames) => createRenderStorageGuard({ durationFrames: frames }),
}) {
  // Check before any bundle, browser, or job-directory work. The estimate is a
  // safety heuristic, while progress monitoring checks the retained reserve.
  const storageGuard = storageGuardFactory(durationFrames);
  storageGuard.assertCanStart();
  onStatus?.({ phase: 'composition', progress: 0 });
  const jobDir = path.join(process.cwd(), '.remotion', 'jobs', jobId);
  await mkdir(jobDir, { recursive: true });
  await mkdir(path.dirname(outputPath), { recursive: true });

  // Build props for the composition
  const inputProps = { clips };
  if (objects && objects.length > 0) {
    inputProps.objects = objects;
  }
  if (texts && texts.length > 0) {
    inputProps.texts = texts;
  }
  if (narrationUrls && narrationUrls.length > 0) inputProps.narration_urls = narrationUrls;
  if (musicUrl) inputProps.music_url = musicUrl;
  if (segmentFrames) inputProps.segment_frames = segmentFrames;
  if (narrationFrames) inputProps.narration_frames = narrationFrames;
  const clipCount = clips.length;
  const checkedRates = validateNumericArray('clipPlaybackRates', clipPlaybackRates, clipCount, (value) => value > 0 && value <= 5);
  const checkedNarrationStarts = validateNumericArray('narrationStartFrames', narrationStartFrames, clipCount, (value) => Number.isInteger(value) && value >= 0);
  if (fadeFrames !== undefined && (!Number.isInteger(fadeFrames) || fadeFrames < 0 || fadeFrames > Math.floor(durationFrames / 2))) {
    throw new Error('fadeFrames must be a non-negative integer no greater than half the render duration');
  }
  const checkedEffects = validateSceneEffects(sceneEffects, clipCount);
  if (checkedRates) inputProps.clip_playback_rates = checkedRates;
  if (fadeFrames !== undefined) inputProps.fade_frames = fadeFrames;
  if (checkedNarrationStarts) inputProps.narration_start_frames = checkedNarrationStarts;
  if (checkedEffects) inputProps.scene_effects = checkedEffects;
  for (const [name, value] of Object.entries({ narration_volume: narrationVolume, music_volume: musicVolume, music_ducked_volume: musicDuckedVolume })) {
    if (value === undefined) continue;
    if (typeof value !== 'number' || !Number.isFinite(value) || value < 0 || value > 1) throw new Error(`${name} must be between 0 and 1`);
    inputProps[name] = value;
  }

  const compositionContents = await buildCompositionSource({ template, compositionCode });
  const compositionHash = sha256(compositionContents);
  const key = bundleKey({ compositionHash, width, height, fps, durationFrames, assetNamespace: publicDir ? jobId : undefined });
  const bundleDir = path.join(process.cwd(), '.remotion', 'bundles', key);
  const entryContents = `import React from 'react';
import { Composition, registerRoot } from 'remotion';
import { DynamicComposition } from './Composition';

const durationInFrames = ${durationFrames};
const fps = ${fps};
const width = ${width};
const height = ${height};

const RemotionRoot: React.FC = () => (
  <>
    <Composition
      id="DynamicComposition"
      component={DynamicComposition}
      durationInFrames={durationInFrames}
      fps={fps}
      width={width}
      height={height}
      defaultProps={{ clips: [], objects: [], texts: [], narration_urls: [], segment_frames: [], narration_frames: [] }}
    />
  </>
);

registerRoot(RemotionRoot);
`;

  let bundled;
  let timeout = null;
  try {
    console.log(`Bundling composition for job ${jobId}...`);
    bundled = await ensureBundle({
      key,
      bundleDir,
      entryContents,
      compositionContents,
      publicDir,
    });

    console.log(`Selecting composition...`);
    const composition = await selectComposition({
      serveUrl: bundled,
      id: 'DynamicComposition',
      inputProps,
    });
    // Acquire before awaiting the browser so an explicit close cannot race a
    // render that has selected its composition but has not entered renderMedia.
    activeRenderCount += 1;
    try {
      const browser = await getBrowser();
      // Bundle and composition setup can consume time, so repeat the heuristic
      // check immediately before Remotion starts its native temporary directory.
      storageGuard.assertCanStart();
      console.log(`Rendering ${durationFrames} frames at ${fps}fps (${width}x${height}), frame concurrency=${FRAME_CONCURRENCY}...`);
      const { cancelSignal, cancel } = makeCancelSignal();
      timeout = timeoutMs ? setTimeout(() => cancel(), timeoutMs) : null;
      try {
        await renderMedia({
          composition,
          serveUrl: bundled,
          codec: 'h264',
          outputLocation: outputPath,
          inputProps,
          concurrency: FRAME_CONCURRENCY,
          puppeteerInstance: browser,
          cancelSignal,
          timeoutInMilliseconds: timeoutMs || undefined,
          onProgress: (nativeProgress) => {
            const { progress } = nativeProgress;
            monitorRenderStorageProgress({
              guard: storageGuard,
              cancel,
              onProgress,
              progress,
            });
            onStatus?.(renderStatusFromNativeProgress({
              progress,
              renderedFrames: nativeProgress.renderedFrames,
              encodedFrames: nativeProgress.encodedFrames,
              stitchStage: nativeProgress.stitchStage,
              totalFrames: durationFrames,
            }));
          },
        });
      } catch (error) {
        if (storageGuard.failure) throw storageGuard.failure;
        throw error;
      }
      if (storageGuard.failure) throw storageGuard.failure;
    } finally {
      activeRenderCount -= 1;
      browserLastUsed = Date.now();
      scheduleBrowserIdleClose();
    }
  } finally {
    if (timeout) {
      clearTimeout(timeout);
    }
    // Monthly renders use a job-unique publicDir. They cannot safely share a bundle
    // with another job, so remove both the generated bundle and its local source.
    if (publicDir) {
      bundleCache.delete(key);
      bundleInFlight.delete(key);
      await Promise.all([
        rm(bundleDir, { recursive: true, force: true }),
        ...(bundled ? [rm(bundled, { recursive: true, force: true })] : []),
      ]);
    }
  }

  console.log(`Render complete: ${outputPath}`);
  return outputPath;
}
