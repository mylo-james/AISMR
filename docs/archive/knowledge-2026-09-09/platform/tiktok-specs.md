# TikTok Technical Specifications

Official dimensions, formats, and limits for TikTok content.

---

## Video Dimensions

### Recommended

| Aspect Ratio | Resolution | Use Case |
|--------------|------------|----------|
| **9:16** (vertical) | 1080 x 1920 | Standard TikTok, Reels-compatible |
| **1:1** (square) | 1080 x 1080 | Cross-platform, less common |
| **16:9** (horizontal) | 1920 x 1080 | Rare on TikTok, YouTube-style |

### Primary Format

**9:16 vertical at 1080x1920** is the standard.

---

## Video Length

### Limits

| Type | Duration |
|------|----------|
| Minimum | 1 second |
| Standard max | 3 minutes |
| Extended max | 10 minutes |
| Optimal for FYP | 15-60 seconds |

### Sweet Spots

| Length | Best For |
|--------|----------|
| 7-15 seconds | Quick jokes, loops, hooks |
| 15-30 seconds | Most content, tutorials |
| 30-60 seconds | Stories, transformations |
| 1-3 minutes | Deep content, loyal audience |

---

## File Specifications

### Format

| Spec | Requirement |
|------|-------------|
| File type | MP4 (preferred), MOV |
| Codec | H.264 |
| Max file size | 287.6 MB (mobile), 500 MB (web) |
| Frame rate | 30 fps (standard), 60 fps (supported) |

### Audio

| Spec | Requirement |
|------|-------------|
| Format | AAC |
| Sample rate | 44.1 kHz |
| Channels | Stereo preferred |

---

## Safe Zones

### UI Overlay Areas (Avoid)

```
+---------------------------------+
|     Top: ~150px                 |  <- Status bar, notch
+---------------------------------+
|                                 |
|                           [!]    |  <- Right: ~120px (icons)
|        SAFE CONTENT AREA        |
|                                 |
|                                 |
+---------------------------------+
|  [!]  Bottom: ~270px             |  <- Caption, controls
|     (username, caption, audio)  |
+---------------------------------+
```

### Safe Zone Summary

For 1080x1920:
- **Top margin**: ~150px
- **Bottom margin**: ~270px
- **Right margin**: ~120px
- **Left margin**: ~60px
- **Safe area**: ~900x1500 centered

### Text Placement

- Keep important text in center 70%
- Captions: 10-15% from bottom edge
- Titles: Upper third, avoid top 8%

---

## Caption Limits

| Element | Limit |
|---------|-------|
| Caption length | 4,000 characters |
| Hashtags | No hard limit, 3-6 recommended |
| Mentions | Up to 5 accounts |

---

## Sound/Music

### TikTok Library

- Millions of licensed tracks
- Trending sounds boost discovery
- Original sounds can go viral

### Original Audio

- Can create original sounds
- Others can reuse your audio
- Label your sound with searchable name

### Copyright

- Licensed library = safe
- Copyrighted music = may be muted/removed
- Commercial accounts have restrictions

---

## Upload Requirements

### From Mobile

- Direct from camera roll
- Record in-app
- Max 287.6 MB

### From Desktop

- TikTok.com/upload
- Max 500 MB
- 1-10 minute videos

### Bulk Upload

- TikTok Creator Portal
- Schedule up to 10 days ahead
- CSV for captions/scheduling

---

## Quality Guidelines

### Video Quality

| Priority | Guideline |
|----------|-----------|
| Resolution | Full HD (1080p) minimum |
| Lighting | Well-lit, avoid dark/grainy |
| Stability | Steady or intentional movement |
| Focus | Sharp, not blurry |

### Audio Quality

| Priority | Guideline |
|----------|-----------|
| Clarity | Clear voice/sounds |
| Volume | Consistent, not clipping |
| Background | Minimal noise |
| Music | Balanced with other audio |

---

## Thumbnail/Cover

### Auto-Generated

- TikTok selects a frame
- Can choose different frame
- Can upload custom cover

### Custom Cover

| Spec | Requirement |
|------|-------------|
| Aspect ratio | 9:16 |
| Resolution | 1080x1920 recommended |
| Format | JPG, PNG |

---

## Algorithm-Friendly Specs

### What TikTok Prefers

| Factor | Recommendation |
|--------|----------------|
| Native upload | Better than re-uploaded |
| No watermarks | Especially not competitor logos |
| High resolution | 1080p minimum |
| Vertical | 9:16 native, not cropped horizontal |
| Clear audio | No muffled/distorted sound |

### What to Avoid

| Issue | Problem |
|-------|---------|
| Low resolution | Algorithm may suppress |
| Other platform watermarks | Definitely suppressed |
| Black bars (letterboxing) | Wastes screen space |
| Horizontal in vertical container | Poor experience |

---

## Quick Reference Card

```
Resolution:     1080 x 1920 (9:16)
Frame rate:     30 fps
Codec:          H.264
Format:         MP4
Max size:       287 MB (mobile) / 500 MB (web)
Length:         15-60 seconds optimal
Safe zone:      Center 70% of frame
Caption:        4,000 chars max
Hashtags:       3-6 recommended
```

---

## Last Updated

2024-12-06
