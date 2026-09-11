# Veo3 Prompting Guide

How to write effective prompts for Veo3 video generation.

---

## Prompt Structure

Use this formula for consistent results:

```
[Cinematography] + [Subject] + [Action] + [Context] + [Style & Ambiance]
```

### Element Breakdown

| Element | What to Include | Example |
|---------|-----------------|---------|
| **Cinematography** | Shot type, camera movement, angle | "Medium shot, slow dolly in" |
| **Subject** | Who/what, with specific details | "a tired corporate worker" |
| **Action** | What's happening | "rubbing his temples in exhaustion" |
| **Context** | Environment, time, setting | "in a cluttered 1980s office at night" |
| **Style** | Lighting, mood, aesthetic | "harsh fluorescent lighting, retro film look" |

### Full Example

```
Medium shot, a tired corporate worker rubbing his temples in exhaustion,
in front of a bulky 1980s computer in a cluttered office late at night.
The scene is lit by harsh fluorescent overhead lights and the green glow
of the monochrome monitor. Retro aesthetic, shot as if on 1980s color film,
slightly grainy.
```

---

## Camera Movement Keywords

Veo3 understands standard cinematography terms:

### Movement Types

| Keyword | Effect | When to Use |
|---------|--------|-------------|
| `static shot` | No camera movement | Stable, calm scenes |
| `pan left/right` | Horizontal rotation | Reveal environment |
| `tilt up/down` | Vertical rotation | Reveal height |
| `dolly in/out` | Move toward/away | Emphasize/isolate subject |
| `tracking shot` | Follow subject sideways | Action sequences |
| `crane shot` | Vertical + horizontal sweep | Epic reveals |
| `aerial/drone shot` | High overhead movement | Establishing shots |
| `handheld` | Deliberate shake | Documentary feel |
| `whip pan` | Fast pan with blur | Quick transitions |
| `arc shot` | Semicircle around subject | Dramatic emphasis |

### Movement Modifiers

- `slow` - Deliberate, calm movement
- `smooth` - Stabilized, professional feel
- `steady` - Minimal shake
- `gliding` - Fluid, ethereal motion

**For ASMR/soothing content**: Use `slow`, `smooth`, `steady` - avoid `handheld` or `whip pan`.

---

## Shot Types and Angles

### Shot Sizes

| Shot | Framing | Best For |
|------|---------|----------|
| `wide shot` | Full environment | Establishing context |
| `full shot` | Head to toe | Character in context |
| `medium shot` | Waist up | Dialogue, interaction |
| `close-up` | Face or object detail | Emotion, emphasis |
| `extreme close-up` | Tiny detail (eyes, texture) | Maximum emphasis |

### Camera Angles

| Angle | Effect | Example |
|-------|--------|---------|
| `eye-level` | Neutral, natural | Default perspective |
| `low-angle` | Subject appears powerful | Hero shots |
| `high-angle` | Subject appears small | Vulnerability |
| `bird's-eye view` | Directly overhead | Maps, patterns |
| `worm's-eye view` | Ground looking up | Exaggerated height |
| `Dutch angle` | Tilted horizon | Unease, tension |
| `over-the-shoulder` | Behind one character | Conversations |
| `POV` | Character's perspective | Immersion |

### Combining Shot + Angle

```
low-angle close-up of the hero's face
high-angle wide shot of the battlefield
```

---

## Style Modifiers That Work

### Camera/Film References

These are "magic words" that trigger specific aesthetics:

| Reference | Result |
|-----------|--------|
| `shot on Arri Alexa` | Clean, cinema-quality look |
| `shot on RED` | Crisp details, slightly cool |
| `35mm film` | Grain, warmth, organic feel |
| `shot on iPhone` | Phone video aesthetic |

**Use one at a time** - don't combine multiple camera references.

### Director/Film Style References

| Reference | Aesthetic |
|-----------|-----------|
| `Wes Anderson style` | Symmetrical, pastel, quirky |
| `David Fincher style` | Moody, controlled, cool palette |
| `Christopher Nolan style` | Epic, dramatic lighting |
| `Blade Runner 2049 cinematography` | Neon, shadows, futuristic |
| `Studio Ghibli style` | Animated, whimsical |

### Color/Mood Modifiers

| Term | Effect |
|------|--------|
| `teal and orange` | Hollywood blockbuster grade |
| `film noir lighting` | High contrast, shadows |
| `golden hour` | Warm, diffused sunset light |
| `high-key` | Bright, even, cheerful |
| `low-key` | Dark, dramatic, moody |
| `desaturated` | Muted, vintage feel |

### What Doesn't Help

These terms are ignored (Veo already outputs high quality):
- `4K`, `8K`, `HD`
- `beautiful`, `masterpiece`
- `award-winning`

---

## Lighting Terms

### Natural Light

| Term | Result |
|------|--------|
| `morning light` | Gentle, warm glow |
| `golden hour` | Beautiful warm diffused light |
| `moonlight` | Faint, bluish, magical |
| `overcast` | Soft, even, no harsh shadows |

### Artificial Light

| Term | Result |
|------|--------|
| `candlelight` | Warm, flickering, dim |
| `neon glow` | Colorful, cyberpunk |
| `fluorescent` | Harsh, unflattering |
| `soft studio lighting` | Professional, even |

### Cinematic Lighting

| Term | Result |
|------|--------|
| `Rembrandt lighting` | Dramatic portrait triangle |
| `volumetric light rays` | Visible shafts of light |
| `backlighting` | Subject silhouette |
| `low-key lighting` | Dark, mysterious |

**For ASMR**: Use `soft warm lighting`, `warm glow`, `gentle ambient lighting`.

---

## Controlling Duration and Pacing

### Duration

- Veo3 outputs 4, 6, or 8 seconds maximum
- Select duration in settings (not in prompt text)
- For longer videos, chain multiple generations

### Pacing Modifiers

| Term | Effect |
|------|--------|
| `slowly`, `leisurely` | Slow tempo |
| `quickly`, `rapid` | Fast tempo |
| `slow-motion` | Time stretched effect |
| `time-lapse` | Time compressed effect |

### Example for Calm Pacing

```
Each movement is slow and deliberate. The overall pacing is unhurried,
with long continuous shots to soothe the viewer.
```

### Multi-Shot Timestamps (Advanced)

For multiple shots in one generation:

```
[00:00-00:02] Medium shot from behind, explorer enters jungle clearing
[00:02-00:04] Camera switches to front, revealing ancient temple
[00:04-00:06] Close-up on her amazed face as sunlight breaks through
```

---

## Aspect Ratio

### Vertical Video (9:16)

For TikTok, explicitly mention:
- `vertical video`
- `9:16 aspect ratio`
- `portrait format`
- `smartphone video`

Or select 9:16 in the generation settings if available.

---

## Prompt Length Sweet Spot

### Recommended Length

- **Minimum**: 30 words (covers basics)
- **Optimal**: 50-150 words (detailed control)
- **Maximum**: ~200 words (beyond this, diminishing returns)

### Structure Tips

1. Lead with camera/shot type
2. Describe subject and action
3. Set environment and context
4. Add lighting and mood
5. Include style reference

Every word should add visual information.

---

## Quick Reference: ASMR Video Prompts

For satisfying, soothing ASMR content:

```
extreme macro perspective, [subject] on [surface].
warm, soft lighting, shot on 100mm macro lens for ultra detail.
slow, steady camera movement. smooth focus.
no music, only the [sound description].
photorealistic, shot on Arri Alexa.
```

### Key ASMR Modifiers

- `extreme macro` - Forces detail focus
- `slow and deliberate` - Calm pacing
- `crystal clear` - Sharp, no blur
- `warm lighting` - Cozy atmosphere
- `no music` - Just the satisfying sound
- `steady` - No camera shake

---

## Common Mistakes

| Mistake | Fix |
|---------|-----|
| Too vague ("mountains") | Add specifics ("snow-capped mountain peaks at sunset") |
| Generic modifiers ("beautiful") | Use specific styles ("golden hour, Alexa look") |
| Missing action | Always include what's happening |
| Conflicting styles | Pick one style reference |
| No lighting specified | Add lighting or time of day |

---

## Last Updated

2024-12-06
