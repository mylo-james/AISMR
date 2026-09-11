# Vertical Video Framing (9:16)

Best practices for composing TikTok-style vertical videos without cut-offs or UI obstruction.

---

## TikTok Safe Zones

TikTok overlays UI elements that can obscure your content. Keep important visuals away from these areas.

### UI Overlay Areas

| Area | What's There | Safe Margin |
|------|--------------|-------------|
| **Bottom** | Caption, audio info, progress bar | Avoid bottom ~20% (~250px) |
| **Right side** | Like, comment, share buttons | Avoid right ~120px |
| **Left side** | Profile photo, username | Avoid bottom-left corner |
| **Top** | Notch, notification bar on some devices | Avoid top ~5-10% (~130px) |

### Safe Zone Summary

For a 1080x1920 frame:
- **Safe area**: Central ~1080x1540 region
- **Rule of thumb**: Keep key content in middle 80% of frame

### Visual Guide

```
+-------------------------+
|     [!] Top margin       |  ~130px
+-------------------------+
|                         |
|                    [!]   |  Right icons
|     [x] SAFE ZONE        |  ~120px
|                         |
|                         |
+-------------------------+
|  [!]  Bottom UI zone     |  ~250px
+-------------------------+
```

---

## Headroom and Lead Room

### Headroom (Space Above Head)

**Do**: Leave small gap above subject's head

**Don't**: Head touching top of frame (any crop = head cut off)

**Guideline**: Position subject's eyes on upper ⅓ line of frame

```
+-------------------------+
|    Headroom (good)      |
|  +-----------------+    |
|  |     👤 Eyes     | <- Upper third line
|  |                 |    |
|  |                 |    |
|  +-----------------+    |
+-------------------------+
```

### Lead Room (Nose Room)

If subject looks/moves toward one side, leave space in that direction.

**Example**: Subject turned left -> position them right of center

```
Subject looking left:
+-------------------------+
|                         |
|    [space]    👤->       |  Subject on right
|                         |
+-------------------------+
```

### Balance

- **Too much headroom**: Subject "sinks" in frame (looks off)
- **Too little headroom**: Cramped feeling, risk of crop
- **Aim for**: Just enough breathing room

---

## Rule of Thirds (Adapted for Vertical)

### Using the Grid

Divide frame into 3x3 grid. Place important elements on lines or intersections.

```
+-------+-------+-------+
|       |   ●   |       |  ● = good spot for eyes
+-------+-------+-------+
|       |       |       |
+-------+-------+-------+
|       |       |       |
+-------+-------+-------+
```

### When to Use Thirds

- Multiple points of interest
- Cinematic feel desired
- Tall objects (place on vertical third line)
- Environmental context needed

### When to Center Instead

For TikTok/Reels, **center composition is often preferred**:
- Talking head videos
- ASMR close-ups
- Symmetrical scenes
- Direct-to-camera content

**Why**: Mimics how viewer holds phone, feels more immersive.

---

## Text-Safe Areas

### Where to Place Text

| Location | Safety | Notes |
|----------|--------|-------|
| **Upper third** | [x] Safest | No UI here |
| **Middle** | [x] Safe | Good for subtitles |
| **Bottom 15-20%** | [ ] Avoid | TikTok caption covers this |
| **Right edge** | [ ] Avoid | Icons cover this |

### Text Placement Guidelines

- Subtitles: **10-12% from bottom edge** (roughly 192-230px up)
- Titles: **~100px from top** (inside safe zone)
- All text: **~60px padding from side edges**
- Center text horizontally when possible

### Text Styling

- Legible font, adequate size (small screens!)
- High contrast with outline/shadow
- White text + dark outline = readable on any background

---

## Centering and Framing Subjects

### Keep Subject Centered

For vertical video (especially AI-generated):
- Centering ensures subject stays in safe zone
- Equal padding reduces cut-off risk
- Good for ASMR: face centered, both sides visible

### Leave Margin for Motion

If subject might move (gestures, dance):
- Frame slightly wider than needed
- Better to have extra background than cut-off hands
- Don't start with limbs touching frame edges

### Don't Cut at Joints

Avoid framing exactly at:
- Knees
- Elbows
- Neck

**Instead**: Go slightly higher/lower or show full body.

### Watch Top and Bottom

- **Top**: Don't cut off forehead
- **Bottom**: Don't let UI cover chin/mouth

---

## Common Mistakes

### 1. Heads Cut Off

**Problem**: Top of head cropped, or forehead missing

**Fix**: Position eyes on upper third, leave headroom

### 2. Text Hidden by UI

**Problem**: Captions or CTAs covered by TikTok buttons

**Fix**: Keep text in central safe zone, above bottom 20%

### 3. Subject Too Close to Edges

**Problem**: Any movement = amputation

**Fix**: Shoot slightly wider, crop in post if needed

### 4. Wasted Vertical Space

**Problem**: Huge empty gap above or below subject

**Fix**: Zoom in or reframe to use the tall frame

### 5. Using 16:9 Without Reframing

**Problem**: Black bars or auto-crop cuts off subjects

**Fix**: Intentionally crop horizontal footage to 9:16

### 6. Not Accounting for Multi-Platform

**Problem**: Works on TikTok, cut off on Instagram

**Fix**: Use most restrictive safe zone (usually TikTok's)

---

## AI Prompting for Vertical Framing

When using AI video generators, explicitly guide the composition.

### Specify Aspect Ratio

```
-ar 9:16                    (Pika Labs)
"vertical 9:16 video"       (general)
"portrait orientation"      (general)
```

### Describe Framing

Include these phrases in prompts:

| Goal | Prompt Phrase |
|------|---------------|
| Centered | `subject is centered in frame` |
| Full body | `full body visible from head to toe` |
| Close-up | `close-up shot of the face, centered` |
| Headroom | `with a little headroom above` |
| Stable | `camera framed steady`, `static camera` |

### Example Prompts

**Talking head**:
```
Vertical 9:16 video, front-facing medium shot of a woman speaking softly.
Subject is well-centered and fully in frame, with a little headroom.
The background is blurred.
```

**ASMR hands**:
```
A smartphone-recorded style portrait video of a chef cooking.
The camera is top-down and vertical, showing the chef's hands centered
as they chop vegetables (full view of the cutting board).
```

### Negative Prompts

If supported, try:
```
-neg: cropped, cut off, half-body, out of frame
```

### Platform Keywords

Include platform reference to trigger appropriate framing:
- `TikTok style video`
- `formatted for TikTok/Reels`
- `smartphone-style vertical shot`

### Consistency Across Shots

For multi-shot sequences, include framing notes in EACH prompt to maintain consistent composition.

---

## Quick Reference

### Pixel Margins (1080x1920)

| Area | Margin |
|------|--------|
| Top | ~130px |
| Bottom | ~250px |
| Right | ~120px |
| Left | ~60px |
| Text from bottom | 192-230px |

### Composition Checklist

- [ ] Subject in center or on thirds
- [ ] Headroom above head
- [ ] Eyes on upper third line
- [ ] Text above bottom 20%
- [ ] Nothing critical on right edge
- [ ] Margin for motion
- [ ] No cuts at joints

---

## Last Updated

2024-12-06
