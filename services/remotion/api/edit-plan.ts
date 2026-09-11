export type SceneEffect = { zoomStart?: number; zoomEnd?: number; panXStart?: number; panXEnd?: number; panYStart?: number; panYEnd?: number; saturation?: number; contrast?: number; vignette?: number };
export type MonthlyEditPlan = { segment_frames?: number[]; clip_playback_rates?: number[]; fade_frames?: number; narration_start_frames?: number[]; scene_effects?: SceneEffect[]; narration_volume?: number; music_volume?: number; music_ducked_volume?: number };
const LIMITS: Record<string, [number, number]> = { zoomStart:[1,1.18],zoomEnd:[1,1.18],panXStart:[-.03,.03],panXEnd:[-.03,.03],panYStart:[-.03,.03],panYEnd:[-.03,.03],saturation:[.8,1.15],contrast:[.9,1.1],vignette:[0,.22] };
const array = (value: unknown, name: string, predicate: (n:number)=>boolean): number[] => { if (!Array.isArray(value) || value.length !== 12 || value.some(v => typeof v !== 'number' || !Number.isFinite(v) || !predicate(v))) throw new Error(`${name} must contain exactly 12 valid values`); return [...value] as number[]; };
export const validateMonthlyEditPlan = (value: unknown): MonthlyEditPlan | undefined => {
 if (value === undefined) return undefined;
 if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('monthly edit_plan must be an object');
 const plan=value as Record<string,unknown>; const allowed=new Set(['segment_frames','clip_playback_rates','fade_frames','narration_start_frames','scene_effects','narration_volume','music_volume','music_ducked_volume']);
 if (Object.keys(plan).some(k=>!allowed.has(k))) throw new Error('monthly edit_plan contains an unsupported field');
 const out: MonthlyEditPlan={};
 if ('segment_frames' in plan) out.segment_frames=array(plan.segment_frames,'segment_frames',n=>Number.isInteger(n)&&n>0&&n<=1800);
 if ('clip_playback_rates' in plan) out.clip_playback_rates=array(plan.clip_playback_rates,'clip_playback_rates',n=>n>0&&n<=5);
 if ('narration_start_frames' in plan) out.narration_start_frames=array(plan.narration_start_frames,'narration_start_frames',n=>Number.isInteger(n)&&n>=0);
 if ('fade_frames' in plan) { if (!Number.isInteger(plan.fade_frames)||Number(plan.fade_frames)<0||Number(plan.fade_frames)>900) throw new Error('fade_frames is invalid'); out.fade_frames=Number(plan.fade_frames); }
 if ('scene_effects' in plan) { if (!Array.isArray(plan.scene_effects)||plan.scene_effects.length!==12) throw new Error('scene_effects must contain exactly 12 objects'); out.scene_effects=plan.scene_effects.map((e,i)=>{ if(!e||typeof e!=='object'||Array.isArray(e)||Object.entries(e).some(([k,v])=>!Object.hasOwn(LIMITS,k)||typeof v!=='number'||!Number.isFinite(v)||v<LIMITS[k][0]||v>LIMITS[k][1])) throw new Error(`scene_effects[${i}] is invalid`); return {...e} as SceneEffect; }); }
 for (const key of ['narration_volume', 'music_volume', 'music_ducked_volume'] as const) {
   if (!(key in plan)) continue;
   const value = plan[key];
   if (typeof value !== 'number' || !Number.isFinite(value) || value < 0 || value > 1) throw new Error(`${key} must be between 0 and 1`);
   out[key] = value;
 }
 return out;
};

export const resolveMonthlyTiming = (plan: MonthlyEditPlan | undefined, sourceDurations: unknown, narrationDurations: unknown, fps: unknown) => {
  if (!Number.isInteger(fps) || Number(fps) <= 0) throw new Error('fps is invalid');
  plan = validateMonthlyEditPlan(plan);
  const source = array(sourceDurations, 'source durations', n => n > 0);
  const narration = Array.isArray(narrationDurations) && narrationDurations.length === 0
    ? [] : array(narrationDurations, 'narration durations', n => n > 0);
  const rates = plan?.clip_playback_rates ?? Array(12).fill(1);
  const maxFrames = source.map((seconds, index) => Math.ceil(seconds / rates[index] * Number(fps)));
  const segmentFrames = plan?.segment_frames ?? maxFrames;
  if (segmentFrames.some((frames, index) => frames > maxFrames[index])) throw new Error('segment_frames exceeds its source after playback rate');
  const starts = plan?.narration_start_frames ?? Array(12).fill(0);
  if (starts.some((start, index) => start >= segmentFrames[index] || (narration.length > 0 && start + Math.ceil(narration[index] * Number(fps)) > segmentFrames[index]))) throw new Error('narration exceeds its segment after offset');
  if (plan?.fade_frames !== undefined && segmentFrames.some(frames => plan.fade_frames! > Math.floor(frames / 2))) throw new Error('fade_frames exceeds half of a segment');
  return { segmentFrames, narrationFrames: narration.map(seconds => Math.ceil(seconds * Number(fps))), narrationStartFrames: starts, clipPlaybackRates: rates, fadeFrames: plan?.fade_frames, sceneEffects: plan?.scene_effects };
};
