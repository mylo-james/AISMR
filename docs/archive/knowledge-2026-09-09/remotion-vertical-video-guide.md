# Remotion Vertical Video Guide (9:16 for TikTok/Instagram Reels)

## Critical: Resolution Format for Vertical Videos

For vertical videos (TikTok, Instagram Reels, YouTube Shorts), configure your Composition with the correct dimensions:

```tsx
<Composition
  id="VerticalVideo"
  component={MyVideo}
  width={1080}          // Width first (smaller dimension)
  height={1920}         // Height second (larger dimension)
  fps={30}
  durationInFrames={300}
  defaultProps={{}}
/>
```

### Common Aspect Ratios

| Platform | Aspect Ratio | Width | Height |
|----------|--------------|-------|--------|
| TikTok/Reels/Shorts | 9:16 | 1080 | 1920 |
| YouTube/Standard | 16:9 | 1920 | 1080 |
| Instagram Feed | 1:1 | 1080 | 1080 |
| Instagram Story | 9:16 | 1080 | 1920 |

---

## Complete 9:16 Vertical Video Example

```tsx
import {
  Composition,
  AbsoluteFill,
  Sequence,
  Series,
  OffthreadVideo,
  useCurrentFrame,
  useVideoConfig,
  interpolate,
  spring,
} from 'remotion';

// Props type
type VerticalVideoProps = {
  clips: string[];
  headers: string[];
};

// Main component
export const VerticalVideo: React.FC<VerticalVideoProps> = ({ clips, headers }) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();

  return (
    <AbsoluteFill style={{ backgroundColor: '#000' }}>
      {/* Video clips layer */}
      <Series>
        {clips.map((clip, index) => (
          <Series.Sequence key={index} durationInFrames={240}>
            <OffthreadVideo
              src={clip}
              style={{
                width: '100%',
                height: '100%',
                objectFit: 'cover',
              }}
            />
          </Series.Sequence>
        ))}
      </Series>

      {/* Text overlay layer */}
      <Series>
        {headers.map((header, index) => (
          <Series.Sequence key={index} durationInFrames={240}>
            <HeaderOverlay text={header} />
          </Series.Sequence>
        ))}
      </Series>
    </AbsoluteFill>
  );
};

// Header overlay component
const HeaderOverlay: React.FC<{ text: string }> = ({ text }) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();

  // Fade in animation
  const opacity = interpolate(frame, [0, 20], [0, 1], {
    extrapolateRight: 'clamp',
  });

  // Slide up animation
  const translateY = spring({
    frame,
    fps,
    config: { damping: 200 },
  });
  const y = interpolate(translateY, [0, 1], [30, 0]);

  return (
    <AbsoluteFill
      style={{
        justifyContent: 'flex-start',
        alignItems: 'center',
        paddingTop: 120, // Safe area from top
      }}
    >
      <div
        style={{
          opacity,
          transform: `translateY(${y}px)`,
          backgroundColor: 'rgba(0, 0, 0, 0.7)',
          color: '#FFFFFF',
          fontSize: 36,
          fontWeight: 'bold',
          fontFamily: 'Montserrat, Arial, sans-serif',
          textTransform: 'uppercase',
          padding: '12px 24px',
          borderRadius: 8,
          textAlign: 'center',
          maxWidth: '90%',
        }}
      >
        {text}
      </div>
    </AbsoluteFill>
  );
};

// Root registration
export const RemotionRoot: React.FC = () => {
  return (
    <Composition
      id="VerticalVideo"
      component={VerticalVideo}
      width={1080}
      height={1920}
      fps={30}
      durationInFrames={720}
      defaultProps={{
        clips: [],
        headers: [],
      }}
    />
  );
};
```

---

## Key Points for Editor Agent

### 1. Dimensions

Always use `width={1080}` and `height={1920}` for 9:16 vertical:

```tsx
<Composition
  width={1080}   // [x] Correct for vertical
  height={1920}  // [x] Correct for vertical
/>
```

**DO NOT** swap width and height - this creates landscape video!

### 2. Video Scaling

Use `objectFit: 'cover'` to fill the frame without letterboxing:

```tsx
<OffthreadVideo
  src={clip}
  style={{
    width: '100%',
    height: '100%',
    objectFit: 'cover',  // [x] Fill frame, crop edges
    // objectFit: 'contain' // Would letterbox
  }}
/>
```

### 3. Safe Areas

Keep text and important content away from edges:

```tsx
// Top safe area (avoid system UI)
paddingTop: 120,

// Bottom safe area (avoid swipe-up areas)
paddingBottom: 150,

// Side safe areas
paddingLeft: 40,
paddingRight: 40,
```

### 4. Text Positioning

For TikTok-style headers at top:

```tsx
<AbsoluteFill style={{
  justifyContent: 'flex-start',  // Top of screen
  alignItems: 'center',          // Horizontally centered
  paddingTop: 120,               // Below status bar
}}>
```

For captions at bottom:

```tsx
<AbsoluteFill style={{
  justifyContent: 'flex-end',    // Bottom of screen
  alignItems: 'center',
  paddingBottom: 150,            // Above swipe area
}}>
```

### 5. Text Styling

Use readable text with contrast:

```tsx
style={{
  color: '#FFFFFF',
  fontSize: 36,                              // Large enough for mobile
  fontWeight: 'bold',
  fontFamily: 'Montserrat, Arial, sans-serif',
  textTransform: 'uppercase',
  textShadow: '0 2px 10px rgba(0,0,0,0.8)', // Or use background
  // OR
  backgroundColor: 'rgba(0, 0, 0, 0.7)',
  padding: '12px 24px',
  borderRadius: 8,
}}
```

### 6. Timing

Standard TikTok pacing:
- **5-15 seconds** per clip for fast-paced content
- **30 frames = 1 second** at 30fps
- Keep total video **15-60 seconds** for optimal engagement

```tsx
// 8 seconds per clip
<Series.Sequence durationInFrames={240}>

// 5 seconds per clip
<Series.Sequence durationInFrames={150}>
```

---

## Common Mistakes to Avoid

[ ] **WRONG**: Swapped dimensions (creates landscape)
```tsx
width={1920}   // Wrong!
height={1080}  // Wrong!
```

[ ] **WRONG**: Using `objectFit: 'contain'` (creates letterboxing)
```tsx
style={{ objectFit: 'contain' }}  // Creates black bars
```

[ ] **WRONG**: Text too small for mobile viewing
```tsx
fontSize: 14  // Too small for phone screens
```

[ ] **WRONG**: Content touching edges (cut off by UI)
```tsx
paddingTop: 0  // Will be hidden by status bar
```

[x] **CORRECT**: Proper vertical configuration
```tsx
<Composition
  width={1080}
  height={1920}
  fps={30}
/>

<OffthreadVideo style={{ objectFit: 'cover' }} />

<div style={{
  fontSize: 36,
  paddingTop: 120,
  paddingBottom: 150,
}}>
```

---

## Multiple Aspect Ratio Support

Create compositions for different platforms:

```tsx
export const RemotionRoot: React.FC = () => {
  return (
    <>
      {/* TikTok/Reels/Shorts - 9:16 Vertical */}
      <Composition
        id="Vertical"
        component={MyVideo}
        width={1080}
        height={1920}
        fps={30}
        durationInFrames={300}
      />

      {/* YouTube - 16:9 Landscape */}
      <Composition
        id="Landscape"
        component={MyVideo}
        width={1920}
        height={1080}
        fps={30}
        durationInFrames={300}
      />

      {/* Instagram Feed - 1:1 Square */}
      <Composition
        id="Square"
        component={MyVideo}
        width={1080}
        height={1080}
        fps={30}
        durationInFrames={300}
      />
    </>
  );
};
```

### Responsive Layout Component

Make layouts adapt to aspect ratio:

```tsx
const ResponsiveLayout: React.FC<{ children: React.ReactNode }> = ({ children }) => {
  const { width, height } = useVideoConfig();
  const isVertical = height > width;
  const isSquare = width === height;

  return (
    <AbsoluteFill
      style={{
        padding: isVertical ? '120px 40px 150px' : '60px 80px',
        justifyContent: isVertical ? 'flex-start' : 'center',
      }}
    >
      {children}
    </AbsoluteFill>
  );
};
```

---

## Performance Tips

### 1. Use OffthreadVideo

```tsx
// Better performance for most cases
<OffthreadVideo src={clip} />

// Only use Video when needed for transparency
<Video src={transparentClip} />
```

### 2. Preload Remote Assets

If using remote URLs, ensure they're accessible and fast:

```tsx
// Good: Direct CDN URLs
<OffthreadVideo src="https://cdn.example.com/video.mp4" />

// Avoid: URLs that redirect or require auth
```

### 3. Optimize Video Sources

Before rendering:
- Compress source videos to reasonable sizes
- Match source resolution to output (1080x1920)
- Use H.264 codec in MP4 container
