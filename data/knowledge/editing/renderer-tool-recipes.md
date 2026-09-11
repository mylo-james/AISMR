---
roles: []
status: legacy
reviewed: 2026-09-09
---
# Renderer tool recipes and supported input

Use these recipes to choose a supported call, not to invent missing inputs.
Replace illustrative example URLs with exact assets from the current handoff.

## Monthly composition

remotion_render accepts template=monthly, clips (12), objects (12), optional
narration_urls (12 when provided), optional music_url, fps=30 and aspect_ratio=9:16.
objects contains item text; January-to-December month headings come from the template.
This example is a complete input shape for a silent batch; add the twelve matching
narration URLs and selected music URL when they are supplied:

```json
{
  "template": "monthly",
  "clips": [
    "https://assets.example/january.mp4", "https://assets.example/february.mp4",
    "https://assets.example/march.mp4", "https://assets.example/april.mp4",
    "https://assets.example/may.mp4", "https://assets.example/june.mp4",
    "https://assets.example/july.mp4", "https://assets.example/august.mp4",
    "https://assets.example/september.mp4", "https://assets.example/october.mp4",
    "https://assets.example/november.mp4", "https://assets.example/december.mp4"
  ],
  "objects": [
    "Blue glass pebble", "Velvet heart", "Emerald teacup", "Jelly rain boot",
    "Crystal tulip", "Cloud seashell", "Glass watermelon", "Amber sunflower",
    "Copper leaf", "Velvet pumpkin", "Marble acorn", "Snow globe"
  ],
  "fps": 30,
  "aspect_ratio": "9:16"
}
```

Narration and music belong to these exact fields, not audio, voice_over or soundtrack.
Do not ask a video model to draw text. Do not send a composition_code when a supported
template covers the edit. Monthly duration is measured by the service; it is not 74s
or an estimate from character counts. The optional audio fields do not create audio.

## Legacy formats

aismr takes exactly the ordered zodiac clips and creative objects supplied by that
workflow; its timeline is 74 seconds. motivational takes its two clips and four
texts; its timeline is 15 seconds. Both use 30 fps. Source instructions and the
actual tool schema override older archived Remotion examples.

## Media access and preflight

Monthly inputs are downloaded once into temporary job assets, probed and rendered
from the same bytes. The service requires an operator-configured exact origin list
in REMOTION_MEDIA_ALLOWED_ORIGINS. A local development asset server needs its exact
scheme, host and port; a hosted source needs its exact HTTPS origin. Redirects,
embedded credentials, oversized files and stalled downloads are rejected.
Report an origin or download error to the operator with the affected asset index.
Do not change the allowlist or substitute another URL to get around the failure.

The template stages real text, narration and music into the final MP4. Technical
verification checks its video/audio streams, portrait dimensions, frame rate,
measured total duration and full decode. It does not transcribe the narration or
visually read the text.

## Inspect a submitted job

Call inspect_render with the returned job_id. The tool is bound to the current run;
it cannot inspect a different run's job. It returns actual status and available
media diagnostics. A pending response is not failure or success. Return its job ID
to the workflow for waiting rather than resubmitting the same render in a loop.

For a completed job, require the actual artifact reference and inspect its technical
verification. For an error, report the indexed source or render diagnostic. Do not
make up a success URL. Fake mode has no verified rendered media.

inspect_render reports the renderer's measurements. It does not watch, listen to,
approve or publish the result. The optional analyze_media tool is a separate paid
still-image analysis capability; it cannot measure an MP4 duration or inspect audio,
and is not needed for this deterministic edit.

## Sources

- src/myloware/tools/remotion.py
- src/myloware/tools/inspect_render.py
- services/remotion/api/server.ts and api/render.ts
- https://ffmpeg.org/ffprobe.html

## Current monthly edit controls

Use the optional `edit_plan` on the monthly renderer tool or editor adapter.
Each supplied scene array must contain exactly twelve entries. `segment_frames`
sets clip length in 30 FPS frames; `clip_playback_rates` changes visual playback
only. `narration_start_frames` places dry speech; `fade_frames` fades each scene
to black at both ends. Preflight rejects source overruns, speech truncation and
fades longer than half a scene.

`scene_effects` supports zoomStart/zoomEnd (1 to 1.18), panXStart/panXEnd and
panYStart/panYEnd (-0.03 to 0.03, also clamped to available crop room), saturation
(0.8 to 1.15), contrast (0.9 to 1.1), and vignette (0 to 0.22). Month and object
labels remain outside the transformed image. Prefer gentle effects that support
existing motion; applying every available effect is unnecessary.

Audio controls are `narration_volume`, `music_volume`, `music_ducked_volume`,
each 0 to 1. Latest defaults are 0.72 voice, 0.23 music in gaps, and 0.12 music
under narration, with smooth duck transitions. Inspect the mix and revise from
listener feedback. The renderer cannot establish spoken-word completeness by
checking durations alone: quiet-gap splitting and listening are separate checks.

Local rendering defaults to one job with one frame worker. Production may
override concurrency. For an audio-only revision, copy the existing encoded
video stream and remix audio locally to avoid another expensive visual render.
