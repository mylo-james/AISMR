# Remotion API Documentation

This documentation was compiled from Context7 using the library ID `/remotion-dev/remotion`.

**This is a comprehensive reference guide for AI agents creating videos with Remotion.**

---

## Table of Contents

1. [Overview](#overview)
2. [Core Concepts](#core-concepts)
3. [Composition Setup](#composition-setup)
4. [Media Components](#media-components)
5. [Animation System](#animation-system)
6. [Sequence and Timing](#sequence-and-timing)
7. [Transitions](#transitions)
8. [Rendering](#rendering)
9. [Best Practices](#best-practices)

---

## Overview

Remotion is a React framework for creating videos programmatically. Instead of using a traditional timeline-based editor, you write React components that describe your video composition. Every frame is a function of React state and props.

### Key Advantages

- **Code-based**: Videos are React components - fully programmable
- **Type-safe**: Full TypeScript support for composition props
- **Reusable**: Components can be shared across compositions
- **Parametrized**: Videos can be dynamically generated from data
- **Self-hosted**: Render on your own infrastructure

### Core Imports

```tsx
import {
  Composition,
  Sequence,
  Series,
  AbsoluteFill,
  useCurrentFrame,
  useVideoConfig,
  interpolate,
  spring,
  staticFile,
} from 'remotion';

import { Video, Audio, Img, OffthreadVideo } from 'remotion';
```

---

## Core Concepts

### Frames vs Time

Remotion works in **frames**, not seconds. Convert between them using fps:

```tsx
const { fps, durationInFrames } = useVideoConfig();

// Seconds to frames
const frames = seconds * fps;

// Frames to seconds
const seconds = frames / fps;

// Example: 30fps video
// 30 frames = 1 second
// 150 frames = 5 seconds
```

### The Current Frame

`useCurrentFrame()` returns the current frame number. This is the foundation of all animations:

```tsx
const frame = useCurrentFrame();
// frame = 0 at start, increments each frame
```

### Video Configuration

`useVideoConfig()` provides composition metadata:

```tsx
const { fps, width, height, durationInFrames } = useVideoConfig();
// fps: 30 (frames per second)
// width: 1080 (pixels)
// height: 1920 (pixels)
// durationInFrames: 300 (total frames)
```

---

## Composition Setup

### Root Component

The root file (`src/Root.tsx`) defines all compositions:

```tsx
import { Composition } from 'remotion';
import { MyVideo } from './MyVideo';

export const RemotionRoot: React.FC = () => {
  return (
    <Composition
      id="MyVideo"
      component={MyVideo}
      durationInFrames={300}      // 10 seconds at 30fps
      width={1080}                 // Vertical video width
      height={1920}                // Vertical video height
      fps={30}
      defaultProps={{
        clips: [],
        title: "Default Title"
      }}
    />
  );
};
```

### Composition Props

Compositions can accept props for dynamic content:

```tsx
type VideoProps = {
  clips: string[];
  title: string;
  duration?: number;
};

export const MyVideo: React.FC<VideoProps> = ({ clips, title }) => {
  return (
    <AbsoluteFill style={{ backgroundColor: '#000' }}>
      {/* Video content */}
    </AbsoluteFill>
  );
};
```

### Dynamic Metadata with calculateMetadata

Calculate duration and other metadata based on props:

```tsx
import { CalculateMetadataFunction } from 'remotion';

export const calculateMetadata: CalculateMetadataFunction<VideoProps> = async ({ props }) => {
  const fps = 30;

  // Calculate total duration from clips
  const totalDuration = props.clips.length * 150; // 5 seconds per clip

  return {
    fps,
    durationInFrames: totalDuration,
    props: {
      ...props,
      // Can modify props here
    },
  };
};

// In Root.tsx
<Composition
  id="MyVideo"
  component={MyVideo}
  calculateMetadata={calculateMetadata}
  defaultProps={{ clips: [] }}
/>
```

---

## Media Components

### Video

Standard video component (blocks main thread):

```tsx
import { Video, staticFile } from 'remotion';

<Video
  src={staticFile('video.mp4')}     // Local file in public/
  src="https://example.com/vid.mp4"  // Or remote URL
  startFrom={30}                     // Start from frame 30 in source
  endAt={150}                        // End at frame 150 in source
  volume={0.8}                       // Audio volume (0-1)
  playbackRate={0.5}                 // Playback speed (0.5 = slow-mo)
  loop                               // Loop the video
  style={{ width: '100%' }}
/>
```

### OffthreadVideo (Recommended)

Better performance for most use cases:

```tsx
import { OffthreadVideo, staticFile } from 'remotion';

<OffthreadVideo
  src={staticFile('video.mp4')}
  startFrom={0}
  volume={0.7}
  style={{
    width: '100%',
    height: '100%',
    objectFit: 'cover'
  }}
/>
```

### Audio

Add background music or sound effects:

```tsx
import { Audio, staticFile } from 'remotion';

<Audio
  src={staticFile('music.mp3')}
  startFrom={0}
  volume={0.5}
/>

// Dynamic volume (fade in/out)
<Audio
  src={staticFile('music.mp3')}
  volume={(frame) => {
    // Fade in over first 30 frames
    if (frame < 30) return interpolate(frame, [0, 30], [0, 1]);
    // Fade out over last 30 frames
    if (frame > 270) return interpolate(frame, [270, 300], [1, 0]);
    return 1;
  }}
/>
```

### Img

Static images:

```tsx
import { Img, staticFile } from 'remotion';

<Img
  src={staticFile('logo.png')}
  style={{
    width: 200,
    position: 'absolute',
    top: 50,
    right: 50,
  }}
/>
```

---

## Animation System

### interpolate()

Map input values to output values:

```tsx
import { interpolate, useCurrentFrame } from 'remotion';

const frame = useCurrentFrame();

// Fade in over 30 frames
const opacity = interpolate(frame, [0, 30], [0, 1]);

// Move from left to center
const x = interpolate(frame, [0, 60], [-200, 0]);

// Multi-point interpolation (fade in, hold, fade out)
const fadeInOut = interpolate(
  frame,
  [0, 30, 120, 150],  // Input keyframes
  [0, 1, 1, 0],        // Output values
  {
    extrapolateLeft: 'clamp',   // Don't go below 0
    extrapolateRight: 'clamp',  // Don't go above 1
  }
);
```

### spring()

Physics-based animations for natural motion:

```tsx
import { spring, useCurrentFrame, useVideoConfig } from 'remotion';

const frame = useCurrentFrame();
const { fps } = useVideoConfig();

// Basic spring (0 to 1)
const scale = spring({
  frame,
  fps,
});

// Customized spring
const bounce = spring({
  frame,
  fps,
  config: {
    damping: 200,       // Higher = less bouncy (default: 10)
    stiffness: 100,     // Higher = snappier (default: 100)
    mass: 0.5,          // Lower = faster (default: 1)
  },
  durationInFrames: 40, // Optional: force specific duration
  delay: 10,            // Optional: delay start by N frames
});

// Use in styles
<div style={{ transform: `scale(${scale})` }}>Hello</div>
```

### interpolateColors()

Animate between colors:

```tsx
import { interpolateColors, useCurrentFrame } from 'remotion';

const frame = useCurrentFrame();

const backgroundColor = interpolateColors(
  frame,
  [0, 75, 150],                      // Keyframes
  ['#ff0000', '#00ff00', '#0000ff'], // Colors
);

<div style={{ backgroundColor }}>Colorful!</div>
```

### Easing Functions

Custom easing curves:

```tsx
import { interpolate, Easing } from 'remotion';

const opacity = interpolate(
  frame,
  [0, 30],
  [0, 1],
  {
    easing: Easing.bezier(0.25, 0.1, 0.25, 1),  // Smooth ease
    // Or built-in:
    // easing: Easing.ease
    // easing: Easing.bounce
    // easing: Easing.elastic(1)
  }
);
```

---

## Sequence and Timing

### Sequence

Control when elements appear:

```tsx
import { Sequence, AbsoluteFill } from 'remotion';

const MyVideo = () => {
  return (
    <AbsoluteFill>
      {/* First 5 seconds (frames 0-150) */}
      <Sequence from={0} durationInFrames={150}>
        <FirstScene />
      </Sequence>

      {/* Seconds 5-10 (frames 150-300) */}
      <Sequence from={150} durationInFrames={150}>
        <SecondScene />
      </Sequence>

      {/* Overlapping sequence (starts frame 120, lasts 60 frames) */}
      <Sequence from={120} durationInFrames={60}>
        <TransitionOverlay />
      </Sequence>
    </AbsoluteFill>
  );
};
```

**Important**: Inside a Sequence, `useCurrentFrame()` returns the frame *relative to that sequence's start*.

```tsx
const FirstScene = () => {
  const frame = useCurrentFrame();
  // frame = 0 when this sequence starts, not when video starts

  const opacity = interpolate(frame, [0, 30], [0, 1]);
  return <div style={{ opacity }}>Fades in from sequence start</div>;
};
```

### Series

Automatically sequence components back-to-back:

```tsx
import { Series } from 'remotion';

const MyVideo = () => {
  return (
    <Series>
      <Series.Sequence durationInFrames={150}>
        <Scene1 />
      </Series.Sequence>

      <Series.Sequence durationInFrames={150}>
        <Scene2 />
      </Series.Sequence>

      <Series.Sequence durationInFrames={150}>
        <Scene3 />
      </Series.Sequence>
    </Series>
  );
};
```

### Putting Videos in Sequence

Render multiple video clips sequentially:

```tsx
import { Series, OffthreadVideo } from 'remotion';

type Props = {
  clips: Array<{ src: string; durationInFrames: number }>;
};

export const VideoSequence: React.FC<Props> = ({ clips }) => {
  return (
    <Series>
      {clips.map((clip, index) => (
        <Series.Sequence key={index} durationInFrames={clip.durationInFrames}>
          <OffthreadVideo src={clip.src} />
        </Series.Sequence>
      ))}
    </Series>
  );
};
```

---

## Transitions

### TransitionSeries

Built-in transition effects:

```tsx
import { TransitionSeries, springTiming } from '@remotion/transitions';
import { slide } from '@remotion/transitions/slide';
import { fade } from '@remotion/transitions/fade';
import { wipe } from '@remotion/transitions/wipe';

const MyVideo = () => {
  return (
    <TransitionSeries>
      <TransitionSeries.Sequence durationInFrames={150}>
        <Scene1 />
      </TransitionSeries.Sequence>

      <TransitionSeries.Transition
        presentation={slide({ direction: 'from-left' })}
        timing={springTiming({
          config: { damping: 200 },
          durationInFrames: 30,
        })}
      />

      <TransitionSeries.Sequence durationInFrames={150}>
        <Scene2 />
      </TransitionSeries.Sequence>

      <TransitionSeries.Transition
        presentation={fade()}
        timing={springTiming({ durationInFrames: 20 })}
      />

      <TransitionSeries.Sequence durationInFrames={150}>
        <Scene3 />
      </TransitionSeries.Sequence>
    </TransitionSeries>
  );
};
```

### Available Transitions

```tsx
import { slide } from '@remotion/transitions/slide';
import { fade } from '@remotion/transitions/fade';
import { wipe } from '@remotion/transitions/wipe';
import { flip } from '@remotion/transitions/flip';
import { clockWipe } from '@remotion/transitions/clock-wipe';

// Slide directions
slide({ direction: 'from-left' })
slide({ direction: 'from-right' })
slide({ direction: 'from-top' })
slide({ direction: 'from-bottom' })

// Wipe directions
wipe({ direction: 'from-left' })
// etc.
```

### Custom Crossfade

Manual crossfade between clips:

```tsx
const Crossfade: React.FC<{ clip1: string; clip2: string }> = ({ clip1, clip2 }) => {
  const frame = useCurrentFrame();
  const { durationInFrames } = useVideoConfig();

  const opacity1 = interpolate(frame, [0, durationInFrames], [1, 0]);
  const opacity2 = interpolate(frame, [0, durationInFrames], [0, 1]);

  return (
    <>
      <AbsoluteFill style={{ opacity: opacity1 }}>
        <OffthreadVideo src={clip1} />
      </AbsoluteFill>
      <AbsoluteFill style={{ opacity: opacity2 }}>
        <OffthreadVideo src={clip2} />
      </AbsoluteFill>
    </>
  );
};
```

---

## Rendering

### Server-Side Rendering

Render videos programmatically:

```typescript
import { bundle } from '@remotion/bundler';
import { renderMedia, selectComposition } from '@remotion/renderer';

// Bundle the Remotion project
const bundled = await bundle({
  entryPoint: './src/index.ts',
  webpackOverride: (config) => config,
});

// Select composition with input props
const composition = await selectComposition({
  serveUrl: bundled,
  id: 'MyVideo',
  inputProps: {
    clips: ['https://example.com/clip1.mp4'],
    title: 'My Title',
  },
});

// Render to MP4
await renderMedia({
  codec: 'h264',
  composition,
  serveUrl: bundled,
  outputLocation: 'out/video.mp4',
  inputProps: {
    clips: ['https://example.com/clip1.mp4'],
    title: 'My Title',
  },
  onProgress: ({ progress }) => {
    console.log(`Progress: ${Math.round(progress * 100)}%`);
  },
  chromiumOptions: {
    enableMultiProcessOnLinux: true,
  },
});
```

### CLI Rendering

```bash
# Render a composition
npx remotion render MyVideo out/video.mp4

# With custom props
npx remotion render MyVideo out/video.mp4 --props='{"title": "Hello"}'

# Specify quality
npx remotion render MyVideo out/video.mp4 --quality=crf18
```

### Docker Rendering

```dockerfile
FROM node:22-bookworm-slim

# Install Chrome dependencies
RUN apt-get update && apt install -y \
  libnss3 libdbus-1-3 libatk1.0-0 libgbm-dev \
  libasound2 libxrandr2 libxkbcommon-dev libxfixes3 \
  libxcomposite1 libxdamage1 libatk-bridge2.0-0 \
  libpango-1.0-0 libcairo2 libcups2

COPY package*.json ./
RUN npm ci

# Install Chrome Headless Shell
RUN npx remotion browser ensure

COPY . .
CMD ["node", "render.mjs"]
```

---

## Best Practices

### 1. Always Export Props Type

```tsx
export type MyVideoProps = {
  clips: string[];
  title: string;
};

export const MyVideo: React.FC<MyVideoProps> = ({ clips, title }) => {
  // ...
};
```

### 2. Use AbsoluteFill for Layouts

```tsx
import { AbsoluteFill } from 'remotion';

const Scene = () => {
  return (
    <AbsoluteFill style={{ backgroundColor: '#000' }}>
      {/* Content fills the entire frame */}
    </AbsoluteFill>
  );
};
```

### 3. Clamp Interpolations

Prevent values from exceeding bounds:

```tsx
const opacity = interpolate(frame, [0, 30], [0, 1], {
  extrapolateLeft: 'clamp',
  extrapolateRight: 'clamp',
});
```

### 4. Use staticFile() for Local Assets

```tsx
import { staticFile } from 'remotion';

// Files in public/ directory
<Video src={staticFile('my-video.mp4')} />
<Img src={staticFile('logo.png')} />
```

### 5. Prefer OffthreadVideo

```tsx
// Better performance
<OffthreadVideo src={videoUrl} />

// Only use Video when you need:
// - Transparent videos
// - Seeking to exact frames
<Video src={videoUrl} />
```

### 6. Handle Missing Data

```tsx
const MyVideo: React.FC<{ clips: string[] }> = ({ clips }) => {
  if (clips.length === 0) {
    return (
      <AbsoluteFill style={{ backgroundColor: '#000' }}>
        <div>No clips provided</div>
      </AbsoluteFill>
    );
  }

  return (
    <Series>
      {clips.map((clip, i) => (
        <Series.Sequence key={i} durationInFrames={150}>
          <OffthreadVideo src={clip} />
        </Series.Sequence>
      ))}
    </Series>
  );
};
```

### 7. Text Styling for Readability

```tsx
const TextOverlay = ({ text }: { text: string }) => (
  <div
    style={{
      fontSize: 72,
      fontWeight: 'bold',
      fontFamily: 'Montserrat, sans-serif',
      color: 'white',
      textShadow: '0 4px 20px rgba(0,0,0,0.8)',
      // Or use a background
      backgroundColor: 'rgba(0,0,0,0.6)',
      padding: '10px 20px',
      borderRadius: 8,
    }}
  >
    {text}
  </div>
);
```

---

## Complete Example

A full video composition with clips, text overlay, and transitions:

```tsx
import {
  AbsoluteFill,
  Sequence,
  Series,
  OffthreadVideo,
  useCurrentFrame,
  useVideoConfig,
  interpolate,
  spring,
} from 'remotion';

type VideoProps = {
  clips: string[];
  title: string;
};

export const MyVideo: React.FC<VideoProps> = ({ clips, title }) => {
  const frame = useCurrentFrame();
  const { fps, durationInFrames } = useVideoConfig();

  // Title animation
  const titleOpacity = interpolate(frame, [0, 30], [0, 1], {
    extrapolateRight: 'clamp',
  });

  const titleScale = spring({
    frame,
    fps,
    config: { damping: 200 },
  });

  return (
    <AbsoluteFill style={{ backgroundColor: '#000' }}>
      {/* Video clips in sequence */}
      <Series>
        {clips.map((clip, index) => (
          <Series.Sequence key={index} durationInFrames={150}>
            <OffthreadVideo
              src={clip}
              style={{ width: '100%', height: '100%', objectFit: 'cover' }}
            />
          </Series.Sequence>
        ))}
      </Series>

      {/* Title overlay */}
      <AbsoluteFill
        style={{
          justifyContent: 'flex-end',
          alignItems: 'center',
          paddingBottom: 150,
        }}
      >
        <div
          style={{
            opacity: titleOpacity,
            transform: `scale(${titleScale})`,
            fontSize: 72,
            fontWeight: 'bold',
            color: 'white',
            textShadow: '0 4px 30px rgba(0,0,0,0.8)',
            textAlign: 'center',
          }}
        >
          {title}
        </div>
      </AbsoluteFill>
    </AbsoluteFill>
  );
};
```

---

## References

- [Remotion Documentation](https://www.remotion.dev/docs)
- [Remotion GitHub](https://github.com/remotion-dev/remotion)
- [Remotion Examples](https://github.com/remotion-dev/template-starter)
