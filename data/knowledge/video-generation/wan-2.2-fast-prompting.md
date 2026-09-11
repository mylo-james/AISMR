---
roles: []
status: legacy
reviewed: 2026-09-09
---
# Wan 2.2 FastWan 5B prompt brief for monthly item clips

**Prepared:** 2026-09-09
**Scope:** Silent, semi-coherent, surreal vertical clips for twelve month/item ideas. This is a prompt-writing brief, not a quality claim, production integration, or authorization to generate media.

## Confirmed endpoint and API surface

Use exactly `fal-ai/wan/v2.2-5b/text-to-video/fast-wan`, the fal Text to Video (FastWan 5B) endpoint. It accepts a required `prompt`, optional `negative_prompt`, `num_frames` (17-161; default 81), `frames_per_second` (4-60; default 24), `seed`, `resolution` (`480p`, `580p`, `720p`; default `720p`), and `aspect_ratio` (`16:9`, `9:16`, `1:1`; default `16:9`). It also exposes prompt expansion, safety checks, guidance, interpolation, and output-encoding settings.

The endpoint schema accepts `num_frames` from 17 through 161. At 24 FPS, 161 frames is about 6.708 seconds. The model-page overview separately says up to five seconds at 720p/24 FPS, so the schema range and overview conflict. The local twelve-scene regeneration verified this exact 480p profile: all twelve returned 161 frames at 24 FPS, 480×832, and 6.708333 seconds, with full decode passing. This is observed endpoint behavior, not a guarantee for every future model version. Set interpolation explicitly if duration/FPS accounting matters: the endpoint otherwise defaults to the `film` interpolator and has an FPS-adjustment behavior when interpolation is used.

For the selected budget tier, fal publicly lists a flat **$0.0125 per 480p output video**. This documents the nominal per-clip price, not a guarantee that any particular account can buy credits in that exact amount, that a request will succeed, or that a result will be accepted.

## Suggested simple request contract

These are product choices, not model-author quality guarantees:

```json
{
  "prompt": "<month/item prompt>",
  "negative_prompt": "on-screen text, subtitles, logos, watermark, letters, numbers, split screen, collage, duplicate subject, deformed object",
  "num_frames": 161,
  "frames_per_second": 24,
  "resolution": "480p",
  "aspect_ratio": "9:16",
  "seed": 42,
  "enable_safety_checker": true,
  "enable_output_safety_checker": true,
  "enable_prompt_expansion": false,
  "guidance_scale": 3.5,
  "interpolator_model": "none",
  "video_quality": "medium",
  "video_write_mode": "balanced"
}
```

The producer request guide uses `num_frames: 161`, its schema maximum, for a roughly 6.7-second source shot at 24 FPS. This setting preserves the existing 480p, 9:16, safety, and no-interpolation profile. The twelve-scene local media proof confirms acceptance and measured duration for this profile; retain that evidence separately from the conflicting overview wording. The API documents default guidance scale 3.5, default output quality `high`, and default write mode `balanced`; the lower `medium` encoding quality and no-interpolation choice above are explicit file-size/simplicity decisions to test. The schema does not publish defaults for the two safety booleans, so set both to `true` rather than relying on omission.

## Prompting guidance (inference, not a supported API parameter)

The official Wan materials establish that a text prompt guides content and that seed controls reproducibility. The following structure is a practical inference for short, low-stakes clips: give the model **one main object**, **one physical action**, **one camera instruction**, and **one simple setting/style phrase**. It limits competing events inside a roughly 6.7-second generation and makes monthly prompts comparable.

Template:

> Vertical 9:16 close-up of **[single recognizable item]**. **[One satisfying, visible action]**. **[One camera move or fixed framing]**. **[Simple surreal material or seasonal setting]**. Soft even lighting, one continuous shot, no people, no readable text.

For AISMR, keep a shared tactile, cinematic dream-world style and the same negative prompt. Each selected object must remain recognizable, but its material, impossible action and miniature setting should vary. Write a compact opening image, one impossible material-driven event and a satisfying final image in one continuous roughly 6.7-second source shot. The editor may deterministically trim it into the chosen pace. This is a creative direction to test, not a guarantee that Wan can follow a miniature screenplay. Avoid plain product turntables and ordinary flexing; also avoid cuts, long plots and stacked transformations. Do not ask it to render month names, labels, captions, dates, menus, or logos. Add labels later with the editor. Do not ask this silent endpoint to produce voiceover, music, or foley; separate narration and licensed background music are separate later stages.

### Examples

**January:**

> Vertical 9:16 macro close-up of a clear ice cube containing a blue glass pebble. The ice slowly cracks open and releases glittering snow. Locked camera, one continuous shot, pale blue winter light, surreal but simple, no readable text.

**April:**

> Vertical 9:16 close-up of a yellow rain boot made of soft translucent jelly. One raindrop lands and the boot gently ripples like water. Slow push-in camera, one continuous shot, clean pastel spring background, no readable text.

**July:**

> Vertical 9:16 close-up of a small watermelon made of polished glass. A single slice opens like a flower and releases floating red bubbles. Locked camera, one continuous shot, warm sunny light, simple surreal object study, no readable text.

**October:**

> Vertical 9:16 close-up of one orange pumpkin carved from velvet. It slowly unzips to reveal tiny glowing autumn leaves, then closes. Slow orbit camera, one continuous shot, dark soft studio background, no readable text.

## Batch record and retry rule

For every generated month, retain the endpoint ID, full input JSON, seed, returned request ID, cost receipt, result URL, and accept/reject reason. Use one request per month. If a clip fails the basic check (recognizable item, one legible action, no unwanted text, vertical framing), retry that clip only with the same batch template and a new recorded seed. A retry adds another flat 480p per-video charge; it does not make quality deterministic.

## Primary sources

1. fal endpoint/model page, capabilities and per-video price, checked 2026-09-09: https://fal.ai/models/fal-ai/wan/v2.2-5b/text-to-video/fast-wan
2. fal endpoint API/schema, checked 2026-09-09: https://fal.ai/models/fal-ai/wan/v2.2-5b/text-to-video/fast-wan/api
3. Wan-AI official model card, Wan2.2 TI2V 5B context: https://huggingface.co/Wan-AI/Wan2.2-TI2V-5B
4. Wan-Video official repository, inference configuration context: https://github.com/Wan-Video/Wan2.2
