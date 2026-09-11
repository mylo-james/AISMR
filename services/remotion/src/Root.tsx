/**
 * Remotion Studio Root Component
 *
 * This file registers compositions for the Remotion Studio preview.
 * Production rendering uses render.mjs with templates from /templates.
 *
 * Available templates:
 * - AISMR: 12 zodiac clips with crossfade transitions
 *
 * Available components:
 * - VideoClip: Basic video playback
 * - AnimatedText: Text with spring animation
 * - Transition: Crossfade between clips
 * - ColorGrade, Vignette: Visual effects
 */

import React from 'react';
import { Composition, AbsoluteFill, Sequence, interpolate, useCurrentFrame, OffthreadVideo } from 'remotion';

// ========== Sample Components for Preview ==========

/**
 * Preview showing the service is working
 */
const ServiceInfo: React.FC = () => {
  const frame = useCurrentFrame();
  const opacity = interpolate(frame, [0, 30], [0, 1], { extrapolateRight: 'clamp' });
  const slideUp = interpolate(frame, [0, 30], [50, 0], { extrapolateRight: 'clamp' });

  return (
    <AbsoluteFill
      style={{
        justifyContent: 'center',
        alignItems: 'center',
        background: 'linear-gradient(135deg, #0f172a 0%, #1e293b 50%, #334155 100%)',
        fontFamily: 'system-ui, -apple-system, sans-serif',
      }}
    >
      <div style={{
        opacity,
        transform: `translateY(${slideUp}px)`,
        textAlign: 'center',
      }}>
        <h1 style={{
          fontSize: 72,
          fontWeight: 800,
          color: 'white',
          margin: 0,
          marginBottom: 20,
          background: 'linear-gradient(90deg, #60a5fa, #a78bfa)',
          WebkitBackgroundClip: 'text',
          WebkitTextFillColor: 'transparent',
        }}>
          AISMR
        </h1>
        <p style={{
          fontSize: 28,
          color: '#94a3b8',
          margin: 0,
        }}>
          Remotion Rendering Service
        </p>
      </div>
    </AbsoluteFill>
  );
};

/**
 * AISMR Zodiac Video Preview
 * This is a preview version of the production AISMR template
 */
const ZODIAC_SIGNS = [
  'Aries', 'Taurus', 'Gemini', 'Cancer', 'Leo', 'Virgo',
  'Libra', 'Scorpio', 'Sagittarius', 'Capricorn', 'Aquarius', 'Pisces'
];

const CLIP_FRAMES = 240;      // 8 seconds at 30fps
const OVERLAP_FRAMES = 60;    // 2 second crossfade
const OFFSET_FRAMES = 180;    // 6 seconds between clip starts

interface ZodiacClipPreviewProps {
  sign: string;
  object: string;
  color: string;
}

const ZodiacClipPreview: React.FC<ZodiacClipPreviewProps> = ({ sign, object, color }) => {
  const frame = useCurrentFrame();

  const opacity = interpolate(
    frame,
    [0, OVERLAP_FRAMES, CLIP_FRAMES - OVERLAP_FRAMES, CLIP_FRAMES],
    [0, 1, 1, 0],
    { extrapolateLeft: 'clamp', extrapolateRight: 'clamp' }
  );

  const textOpacity = interpolate(
    frame,
    [OVERLAP_FRAMES, OVERLAP_FRAMES + 20, CLIP_FRAMES - OVERLAP_FRAMES - 20, CLIP_FRAMES - OVERLAP_FRAMES],
    [0, 1, 1, 0],
    { extrapolateLeft: 'clamp', extrapolateRight: 'clamp' }
  );

  // Gentle zoom animation
  const scale = interpolate(frame, [0, CLIP_FRAMES], [1, 1.05], {
    extrapolateRight: 'clamp',
  });

  return (
    <AbsoluteFill style={{ opacity }}>
      {/* Gradient background simulating video */}
      <AbsoluteFill style={{
        background: `linear-gradient(${frame}deg, ${color}88, ${color}44)`,
        transform: `scale(${scale})`,
      }}>
        {/* Zodiac symbol placeholder */}
        <AbsoluteFill style={{
          justifyContent: 'center',
          alignItems: 'center',
        }}>
          <div style={{
            fontSize: 200,
            opacity: 0.1,
            color: 'white',
          }}>
            ♈
          </div>
        </AbsoluteFill>
      </AbsoluteFill>

      {/* Text overlay */}
      <AbsoluteFill style={{
        justifyContent: 'center',
        alignItems: 'center',
        // Match production: place text in upper 2/3.
        paddingTop: 120,
        paddingBottom: 700,
        opacity: textOpacity,
      }}>
        <div style={{
          color: 'white',
          textAlign: 'center',
          textShadow: '3px 3px 15px rgba(0,0,0,0.95), 0 0 40px rgba(0,0,0,0.6)',
          padding: '0 40px',
          fontFamily: 'system-ui, -apple-system, sans-serif',
          overflowWrap: 'break-word',
          wordBreak: 'break-word',
        }}>
          <div style={{ fontSize: 76, fontWeight: 800, lineHeight: 1.05 }}>
            {object}
          </div>
          <div style={{
            fontSize: 56,
            fontWeight: 700,
            lineHeight: 1.1,
            marginTop: 10,
            letterSpacing: '0.06em',
            textTransform: 'uppercase',
            opacity: 0.95,
          }}>
            {sign}
          </div>
        </div>
      </AbsoluteFill>
    </AbsoluteFill>
  );
};

interface AISMRPreviewProps {
  clips?: string[];
  objects?: string[];
}

const AISMRPreview: React.FC<AISMRPreviewProps> = ({ clips = [], objects = [] }) => {
  // Generate colors for preview when no clips provided
  const colors = [
    '#ef4444', '#f97316', '#eab308', '#84cc16', '#22c55e', '#14b8a6',
    '#06b6d4', '#3b82f6', '#6366f1', '#8b5cf6', '#a855f7', '#ec4899',
  ];

  const sampleObjects = [
    'Ruby Flame', 'Golden Horn', 'Twin Mirrors', 'Pearl Shell', 'Solar Mane', 'Crystal Wheat',
    'Rose Quartz', 'Obsidian Sting', 'Amber Arrow', 'Onyx Peak', 'Aqua Wave', 'Opal Scale',
  ];

  return (
    <AbsoluteFill style={{ backgroundColor: 'black' }}>
      {ZODIAC_SIGNS.map((sign, index) => (
        <Sequence
          key={sign}
          from={index * OFFSET_FRAMES}
          durationInFrames={CLIP_FRAMES}
        >
          <ZodiacClipPreview
            sign={sign}
            object={objects[index] || sampleObjects[index]}
            color={colors[index]}
          />
        </Sequence>
      ))}
    </AbsoluteFill>
  );
};

/**
 * Video with Crossfade Demo
 * Shows the crossfade transition effect between two clips
 */
interface CrossfadeDemoProps {
  clipA?: string;
  clipB?: string;
}

const CrossfadeDemo: React.FC<CrossfadeDemoProps> = ({ clipA, clipB }) => {
  const frame = useCurrentFrame();

  // First clip: fully visible at start, fades out
  const opacityA = interpolate(
    frame,
    [0, 60, 120, 180],
    [1, 1, 0, 0],
    { extrapolateRight: 'clamp' }
  );

  // Second clip: fades in, fully visible at end
  const opacityB = interpolate(
    frame,
    [60, 120, 180, 240],
    [0, 1, 1, 1],
    { extrapolateRight: 'clamp' }
  );

  return (
    <AbsoluteFill style={{ backgroundColor: 'black' }}>
      {/* Clip A */}
      <AbsoluteFill style={{ opacity: opacityA }}>
        {clipA ? (
          <OffthreadVideo src={clipA} style={{ width: '100%', height: '100%', objectFit: 'cover' }} />
        ) : (
          <AbsoluteFill style={{ background: 'linear-gradient(135deg, #3b82f6, #1e40af)', justifyContent: 'center', alignItems: 'center' }}>
            <span style={{ fontSize: 48, color: 'white', fontFamily: 'system-ui' }}>Clip A</span>
          </AbsoluteFill>
        )}
      </AbsoluteFill>

      {/* Clip B */}
      <AbsoluteFill style={{ opacity: opacityB }}>
        {clipB ? (
          <OffthreadVideo src={clipB} style={{ width: '100%', height: '100%', objectFit: 'cover' }} />
        ) : (
          <AbsoluteFill style={{ background: 'linear-gradient(135deg, #8b5cf6, #6d28d9)', justifyContent: 'center', alignItems: 'center' }}>
            <span style={{ fontSize: 48, color: 'white', fontFamily: 'system-ui' }}>Clip B</span>
          </AbsoluteFill>
        )}
      </AbsoluteFill>

      {/* Progress indicator */}
      <AbsoluteFill style={{ justifyContent: 'flex-end', paddingBottom: 40 }}>
        <div style={{
          height: 4,
          background: '#333',
          margin: '0 40px',
          borderRadius: 2,
        }}>
          <div style={{
            height: '100%',
            width: `${(frame / 240) * 100}%`,
            background: 'linear-gradient(90deg, #3b82f6, #8b5cf6)',
            borderRadius: 2,
          }} />
        </div>
      </AbsoluteFill>
    </AbsoluteFill>
  );
};

// ========== Root Registration ==========

export const Root: React.FC = () => {
  return (
    <>
      {/* Service info splash screen */}
      <Composition
        id="ServiceInfo"
        component={ServiceInfo}
        durationInFrames={150}
        fps={30}
        width={1080}
        height={1920}
        defaultProps={{}}
      />

      {/* AISMR Template Preview (full 74 second video) */}
      <Composition
        id="AISMR"
        component={AISMRPreview}
        durationInFrames={2220}  // 74 seconds at 30fps
        fps={30}
        width={1080}
        height={1920}
        defaultProps={{
          clips: [],
          objects: [],
        }}
      />

      {/* Crossfade Demo (8 seconds showing transition) */}
      <Composition
        id="CrossfadeDemo"
        component={CrossfadeDemo}
        durationInFrames={240}
        fps={30}
        width={1080}
        height={1920}
        defaultProps={{
          clipA: undefined,
          clipB: undefined,
        }}
      />
    </>
  );
};
