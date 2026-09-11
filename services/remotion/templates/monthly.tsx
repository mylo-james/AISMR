/** Fixed monthly editor template: 12 clips, optional dry narration and music. */
const MONTHS = ['January', 'February', 'March', 'April', 'May', 'June', 'July', 'August', 'September', 'October', 'November', 'December'];
const SAFE = { top: 0.07, bottom: 0.16, horizontal: 0.07 };

type SceneEffect = { zoomStart?: number; zoomEnd?: number; panXStart?: number; panXEnd?: number; panYStart?: number; panYEnd?: number; saturation?: number; contrast?: number; vignette?: number };
type MonthlyProps = { clips: string[]; objects: string[]; narration_urls?: string[]; music_url?: string; segment_frames: number[]; narration_frames?: number[]; clip_playback_rates?: number[]; fade_frames?: number; narration_start_frames?: number[]; scene_effects?: SceneEffect[]; narration_volume?: number; music_volume?: number; music_ducked_volume?: number };
const clamp = (value: number, minimum: number, maximum: number) => Math.max(minimum, Math.min(value, maximum));
const frames = (value: number | undefined, maximum: number) => Number.isFinite(value) ? clamp(Math.floor(value ?? 0), 0, maximum) : 0;
const effectValue = (value: number | undefined, fallback: number, minimum: number, maximum: number) => Number.isFinite(value) ? clamp(value ?? fallback, minimum, maximum) : fallback;

const MusicBed: React.FC<{ src: string; segmentFrames: number[]; narrationFrames: number[]; narrationStartFrames: number[]; fadeFrames: number; musicVolume: number; musicDuckedVolume: number }> = ({ src, segmentFrames, narrationFrames, narrationStartFrames, fadeFrames, musicVolume, musicDuckedVolume }) => {
  const frame = useCurrentFrame();
  const { durationInFrames } = useVideoConfig();
  const edgeFrames = frames(fadeFrames, Math.floor(durationInFrames / 2));
  let cursor = 0;
  let narrationEnvelope = 0;
  for (let index = 0; index < segmentFrames.length; index += 1) {
    const start = cursor + frames(narrationStartFrames[index], segmentFrames[index] ?? 0);
    const end = start + (narrationFrames[index] ?? 0);
    if (end > start) narrationEnvelope = Math.max(narrationEnvelope, Math.min(
      interpolate(frame, [start - 8, start], [0, 1], { extrapolateLeft: 'clamp', extrapolateRight: 'clamp' }),
      interpolate(frame, [end, end + 8], [1, 0], { extrapolateLeft: 'clamp', extrapolateRight: 'clamp' }),
    ));
    cursor += segmentFrames[index] ?? 0;
  }
  const edge = edgeFrames === 0 ? 1 : Math.min(interpolate(frame, [0, edgeFrames], [0, 1], { extrapolateRight: 'clamp' }), interpolate(frame, [Math.max(0, durationInFrames - edgeFrames), durationInFrames], [1, 0], { extrapolateLeft: 'clamp' }));
  return <Audio src={src} loop volume={edge * interpolate(narrationEnvelope, [0, 1], [musicVolume, musicDuckedVolume])} />;
};

const Segment: React.FC<{ month: string; clip: string; object: string; durationInFrames: number; narrationUrl?: string; narrationStartFrames: number; playbackRate: number; fadeFrames: number; sceneEffect?: SceneEffect; narrationVolume: number }> = ({ month, clip, object, durationInFrames, narrationUrl, narrationStartFrames, playbackRate, fadeFrames, sceneEffect, narrationVolume }) => {
  const frame = useCurrentFrame();
  const { width, height } = useVideoConfig();
  const fade = frames(fadeFrames, Math.floor(durationInFrames / 2));
  const opacity = fade === 0 ? 1 : Math.min(interpolate(frame, [0, fade], [0, 1], { extrapolateRight: 'clamp' }), interpolate(frame, [Math.max(0, durationInFrames - fade), durationInFrames], [1, 0], { extrapolateLeft: 'clamp' }));
  const progress = interpolate(frame, [0, Math.max(1, durationInFrames - 1)], [0, 1], { extrapolateRight: 'clamp' });
  const zoomStart = effectValue(sceneEffect?.zoomStart, 1, 1, 1.18);
  const zoomEnd = effectValue(sceneEffect?.zoomEnd, 1, 1, 1.18);
  const zoom = interpolate(progress, [0, 1], [zoomStart, zoomEnd]);
  const clampPan = (value: number, scale: number) => clamp(value, -Math.min(0.03, (scale - 1) / (2 * scale)), Math.min(0.03, (scale - 1) / (2 * scale)));
  const panX = interpolate(progress, [0, 1], [clampPan(effectValue(sceneEffect?.panXStart, 0, -0.03, 0.03), zoomStart), clampPan(effectValue(sceneEffect?.panXEnd, 0, -0.03, 0.03), zoomEnd)]);
  const panY = interpolate(progress, [0, 1], [clampPan(effectValue(sceneEffect?.panYStart, 0, -0.03, 0.03), zoomStart), clampPan(effectValue(sceneEffect?.panYEnd, 0, -0.03, 0.03), zoomEnd)]);
  const narrationStart = frames(narrationStartFrames, Math.max(0, durationInFrames - 1));
  const rate = Number.isFinite(playbackRate) && playbackRate > 0 ? playbackRate : 1;
  return <AbsoluteFill style={{ backgroundColor: 'black', opacity }}>
    <AbsoluteFill style={{ overflow: 'hidden' }}>
      <OffthreadVideo src={staticFile(clip)} volume={0} playbackRate={rate} style={{ width: '100%', height: '100%', objectFit: 'cover', transform: `scale(${zoom}) translate(${panX * 100}%, ${panY * 100}%)`, filter: `saturate(${effectValue(sceneEffect?.saturation, 1, 0.8, 1.15)}) contrast(${effectValue(sceneEffect?.contrast, 1, 0.9, 1.1)})` }} />
      <AbsoluteFill style={{ pointerEvents: 'none', background: 'radial-gradient(circle, transparent 48%, black 100%)', opacity: effectValue(sceneEffect?.vignette, 0, 0, 0.22) }} />
    </AbsoluteFill>
    <AbsoluteFill style={{ justifyContent: 'flex-start', padding: `${Math.round(height * SAFE.top)}px ${Math.round(width * SAFE.horizontal)}px 0`, pointerEvents: 'none' }}><div style={{ color: 'white', fontFamily: 'system-ui, sans-serif', textAlign: 'center', textShadow: '0 3px 12px #000', fontSize: Math.round(width * 0.047), fontWeight: 700, letterSpacing: Math.round(width * 0.003), textTransform: 'uppercase', overflowWrap: 'break-word', wordBreak: 'break-word' }}>{month}</div></AbsoluteFill>
    <AbsoluteFill style={{ justifyContent: 'flex-end', padding: `0 ${Math.round(width * SAFE.horizontal)}px ${Math.round(height * SAFE.bottom)}px`, pointerEvents: 'none' }}><div style={{ color: 'white', fontFamily: 'system-ui, sans-serif', textAlign: 'center', textShadow: '0 3px 12px #000', overflowWrap: 'break-word', wordBreak: 'break-word', fontSize: Math.round(width * 0.07), fontWeight: 800, lineHeight: 1.05 }}>{object}</div></AbsoluteFill>
    {narrationUrl ? <Sequence from={narrationStart} durationInFrames={Math.max(1, durationInFrames - narrationStart)}><Audio src={staticFile(narrationUrl)} volume={narrationVolume} /></Sequence> : null}
  </AbsoluteFill>;
};

export const RemotionComposition: React.FC<MonthlyProps> = ({ clips, objects, narration_urls = [], music_url, segment_frames, narration_frames = [], clip_playback_rates = [], fade_frames = 0, narration_start_frames = [], scene_effects = [], narration_volume = 0.72, music_volume = 0.23, music_ducked_volume = 0.12 }) => {
  let start = 0;
  const sequences = MONTHS.map((month, index) => {
    const from = start;
    const durationInFrames = segment_frames[index] ?? 1;
    start += durationInFrames;
    return <Sequence key={month} from={from} durationInFrames={durationInFrames}><Segment month={month} clip={clips[index] ?? ''} object={objects[index] ?? ''} durationInFrames={durationInFrames} narrationUrl={narration_urls[index]} narrationStartFrames={narration_start_frames[index] ?? 0} playbackRate={clip_playback_rates[index] ?? 1} fadeFrames={fade_frames} sceneEffect={scene_effects[index]} narrationVolume={narration_volume} /></Sequence>;
  });
  return <AbsoluteFill>{sequences}{music_url ? <MusicBed src={staticFile(music_url)} segmentFrames={segment_frames} narrationFrames={narration_frames} narrationStartFrames={narration_start_frames} fadeFrames={fade_frames} musicVolume={music_volume} musicDuckedVolume={music_ducked_volume} /> : null}</AbsoluteFill>;
};
