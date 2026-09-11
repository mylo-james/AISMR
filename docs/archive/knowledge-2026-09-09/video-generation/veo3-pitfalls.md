# Veo3 Pitfalls & Troubleshooting

Common mistakes, failure modes, workarounds, and content limits.

---

## Prompt-Caused Artifacts

### Over-Styling

**Problem**: Too many style adjectives or conflicting instructions cause visual shimmer/distortion.

**Why**: Descriptors "fight" each other, model can't satisfy all.

**Fix**:

- Treat prompts like a shot list (subject, action, camera, lighting)
- Add stylistic flourishes one at a time
- If not working, remove parts rather than adding more

### Conflicting Instructions

**Problem**: Contradictory elements cause generation failures or weird glitches.

**Example**: "static camera" + "dynamic tracking shot"

**Fix**: Keep prompts focused and clear. Remove ambiguous parts.

---

## Body and Anatomy Issues

### Limb Distortions

**Symptoms**:

- Warped limbs during motion
- Characters with two right arms
- Twisted joints
- Extra fingers

**When it happens**: Rapid or complex body movements

**Fixes**:

- Slow down the motion
- Use closer shots (less body visible)
- Add negative prompts: `deformed, bad anatomy, extra limb, extra fingers`
- Use reference image with clear anatomy

### Cut-Off Head Problem (Vertical Video)

**Symptoms**: Person's head cut off at video start, "pops in" later

**Especially common in**: 9:16 vertical API generations

**Fixes**:

1. Add to negative prompt: `cropped head, cut off head, out of frame, headless body`
2. Prompt: `full body shot`, `wide-angle full body`, `camera at eye level`
3. Start with close-up on face, then pull back to full body
4. Describe composition literally: `the entire person is visible from head to toe`

**Note**: Even with fixes, face may change during video (known limitation).

---

## Flickering and Temporal Inconsistency

### Causes

- Temporal drift (model doesn't keep features consistent frame-to-frame)
- Overly complex styling that varies across clip
- Too many style elements

### Fixes

1. **Lock settings**: Use fixed seed if API allows
2. **Simplify prompt**: Fewer style elements = more consistent frames
3. **Post-process**: Light denoise or motion interpolation filter
4. **For static scenes**: Explicitly say `camera remains perfectly still`, negative: `no camera movement`

### Static Scene Issue

**Problem**: Model expects motion, so "nothing happens" scenes cause unwanted camera drift.

**Fix**: Add `tripod shot` or `security camera footage` to imply static view.

---

## Challenging Subjects

### Faces

**Problems**: Distorted, "AI-looking" faces, wobbling features during movement

**Best practices**:

- Good lighting and close-ups
- Describe face clearly in prompt
- Use image reference for stable face
- Keep face still if possible

### Hands

**Problems**: Melted appearance, wrong number of fingers, bent wrong ways

**Best practices**:

- Avoid showing hands in intricate positions
- Keep hands partially out of frame
- Use reference image with clear hand pose

### Text

**Problem**: Gibberish characters, "alien language" on signs

**Current state**: After mid-2025 update, nearly impossible to get readable text

**Workarounds**:

- Put exact text in quotation marks in prompt (signals literal phrase)
- Keep text SHORT (single word better than sentence)
- Place text on flat, front-facing sign, well-lit, close-up
- **Best approach**: Generate scene with blank sign, overlay real text in editing

### Other Difficult Subjects

| Subject                         | Issue                     | Workaround                  |
| ------------------------------- | ------------------------- | --------------------------- |
| Fine patterns (shirts, watches) | Flicker/change each frame | Avoid detailed patterns     |
| Reflective surfaces             | Morph unpredictably       | Simplify reflections        |
| Fast action                     | Blur, smearing            | Keep motions slow and clear |
| Complex physics (water, cloth)  | Unrealistic behavior      | Accept or edit in post      |

---

## Aspect Ratio Issues

### 9:16 Vertical Problems

| Issue                  | Cause                   | Fix                                 |
| ---------------------- | ----------------------- | ----------------------------------- |
| Head cut off           | API vs UI inconsistency | Use UI, or explicit framing prompts |
| Lower quality at edges | Model bias              | Check latest model version          |
| Wrong aspect output    | Default to 16:9         | Explicitly say `vertical 9:16`      |

### API vs UI Difference

Same prompt may behave differently:

- **Gemini UI**: Often works correctly
- **API**: May have cropping issues

Test in UI first, then transfer to API if needed.

---

## Duration Limits

### Hard Limits

- **Maximum per generation**: 8 seconds
- **Options**: 4, 6, or 8 seconds
- Cannot generate 30+ second video in one go

### For Longer Videos

1. Generate multiple 6-8 second clips
2. Stitch together in editing
3. Use "save frame as asset" to carry over last frame for continuity
4. Color grade final edit to smooth differences

### Continuity Warning

Between scenes, character/lighting can shift. Even with frame carryover, expect:

- Slight color/contrast shifting
- Need for post color grading

---

## Regenerate vs. Refine

### When to Regenerate (Same Prompt)

- One-off glitch (bizarre single frame, weird saturation)
- Output was close but had minor random issues
- Want a different "take" of same scene

### When to Adjust Prompt

- Consistent problem across multiple generations
- Model keeps ignoring specific instructions
- Unwanted element appears every time
- Wrong camera angle consistently

### The 1000 Credit Lesson

If error persists across regenerations despite detailed prompting, the model has a bias/limitation. Simplify the idea or break into smaller pieces.

### Iteration Strategy

1. Generate with initial prompt
2. Identify what's wrong
3. Add specific fix (or negative prompt)
4. Regenerate
5. If same issue, adjust approach not detail level
6. **Save working prompts** - reproduce success, don't start from scratch

---

## Rate Limits and Quotas

### Hidden Daily Limits

| Interface           | Limit                | Error Message          |
| ------------------- | -------------------- | ---------------------- |
| Gemini App (mobile) | 3 videos/24 hours    | "Something went wrong" |
| Google Vids web     | ~20 videos/day       | "limit reached"        |
| Google Flow (Labs)  | Higher, but unstable | Service unavailable    |

### API Limits

- 429 Too Many Requests if too many jobs
- Organization-level quotas per day
- Check Google Cloud quota page

### Quality Degradation

Some users report quality drops after heavy use:

- Extremely low-res outputs
- HDR-blasted videos
- May be stealth throttling or load-shedding

### Practical Tips

- Pace generations over multiple days
- Use "Fast" mode for drafts (may count differently)
- "Something went wrong" after multiple tries = stop for the day
- Plan important generations when well within limits

---

## Content Moderation Gotchas

### What Gets Blocked

| Content                     | Likely Result              |
| --------------------------- | -------------------------- |
| Real person's face in image | "Celebrity likeness" error |
| Professional model photo    | May think it's a celebrity |
| Any image with minors       | Blocked immediately        |
| Violence, gore              | Refused                    |
| Copyrighted images          | Blocked                    |
| Real company/people names   | May be flagged             |

### Overzealous Filter

The system is very sensitive to prevent deepfakes:

- Stock photo faces sometimes flagged as celebrities
- Normal fashion model images rejected
- Even AI-generated faces sometimes blocked

### Workarounds

- Use AI-generated reference images (from Midjourney, etc.) not real photos
- Phrase prompts to avoid flag words
- If image blocked, try without that image
- Change character to adult if minor suspected
- Remove real names

### Mid-Generation Stops

Filter can halt generation partway through:

- One "bad" scene stops whole multi-scene project
- May get vague "cannot generate" with no clear reason

**Debug approach**: Remove elements one at a time to find trigger.

---

## Effective Workarounds Summary

### Negative Prompting

Essential for avoiding glitches:

```
blurry, low quality, deformed face, extra fingers, inconsistent outfit,
flickering, cropped head, cut off head, out of frame, changing facial
features, different face, bad anatomy, extra limb
```

### Reference Images

- Use for critical character/object details
- AI-generated images safer than real photos
- Helps lock in style/character

### First Frame Technique

1. Generate high-quality keyframe (even from Midjourney/SD)
2. Use as first frame in Veo3
3. Veo generates motion from locked-in starting point

### Dialogue/Speech

- Include speech in quotes: `"Hello, how are you?" said the man`
- Triggers lip-sync attempt
- Don't use quotes if you don't want subtitles

### Lighting Consistency

If brightness ramps up between scenes:

- Add: `lighting remains soft and even throughout`
- Color grade in post to smooth differences

### Audio Tips

- Explicitly describe sounds: `quiet tapping sound`, `soft whispering`
- For better quality: mute and overlay separately recorded audio
- Ensure `generateAudio: true` in API

---

## Quick Diagnosis Table

| Symptom                | Likely Cause       | Action                                 |
| ---------------------- | ------------------ | -------------------------------------- |
| "Something went wrong" | Daily quota hit    | Wait 24 hours                          |
| Distorted limbs        | Complex motion     | Slow down, tighter shot                |
| Head cut off           | 9:16 framing bug   | Add negative prompts, explicit framing |
| Flickering             | Too many styles    | Simplify prompt                        |
| Gibberish text         | Model limitation   | Add in post-production                 |
| "Celebrity likeness"   | Real face detected | Use AI-generated face                  |
| Can't generate         | Content filter     | Remove flagged content                 |
| Low quality output     | Throttling or bug  | Try again tomorrow                     |
| Same error repeating   | Prompt issue       | Change approach, not add detail        |

---

## Last Updated

2024-12-06
