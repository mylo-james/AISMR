---
roles: []
status: legacy
reviewed: 2026-09-09
---
# Editor: complete the edit from supplied assets

The editor owns timing, on-screen text, audio placement, mixing and the final
composition. The video model supplies clean footage without rendered words.
Add exact month and item labels as deterministic text layers, using the same
item spelling as the narration brief. Do not regenerate footage or publish.

## Choose the supported composition

The monthly template is for twelve clips in January-to-December order with
twelve item labels, optional twelve narration files and optional background music.
The renderer measures source durations, fits one scene to each clip and checks
whether narration fits. It owns frame arithmetic and audio automation.
Read editing/renderer-tool-recipes.md for exact arguments and job inspection.

The legacy aismr template remains a zodiac format with a fixed 74-second timeline.
Motivational remains two clips and four text overlays with a 15-second timeline.
Do not use either fixed timeline for short monthly Wan clips. Template mode uses
30 fps and owns its duration; setting duration_seconds does not resize that timeline.
Use the profile supplied by the workflow. The current recovered aismr workflow
still supplies zodiac data; the new month/video/narration asset handoff must be
provided explicitly until the production workflow migration is implemented.

## Text

Pass the approved item labels through objects in matching clip order. In monthly
mode the template adds the month name, readable item text, contrast and margins.
Text is a real overlay, not a video-model prompt. Preserve punctuation and spelling;
do not substitute an invented label for a missing value. Check a long label and
every month in the rendered preview for overflow, readability and ordering.

## Narration and music

Pass supplied narration_urls in exactly the same order. These are audio files,
not spoken text or video-model voice_over hints. The monthly template mutes source
video audio, places narration with its scene and reduces the music under speech.
The music bed loops or ends with the composition and fades at its boundaries.
If narration exceeds its clip, report the indexed mismatch to the producer or
operator for a corrected asset. Do not silently cut off words or regenerate media.

Use music with supplied, verified rights. Retain source and license evidence in
the asset handoff. CC0 permits copying and adaptation including commercial use;
verify the individual recording. Royalty-free does not mean free or unrestricted.

## Completion and limits

A queued render has a job ID, not a final MP4. The workflow owns waiting and retry
scheduling. Inspect the returned job for status, measured media diagnostics and
the final artifact. Decode/metadata checks establish technical validity; they do
not prove the words were spoken correctly, text is visible or mixing sounds good.
Review the preview before presenting it for the existing publication decision.

## Sources

- src/myloware/tools/remotion.py and inspect_render.py
- services/remotion/templates/monthly.tsx and api/render.ts
- https://www.remotion.dev/docs/sequence
- https://www.remotion.dev/docs/html5-audio
- https://creativecommons.org/publicdomain/zero/1.0/

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
