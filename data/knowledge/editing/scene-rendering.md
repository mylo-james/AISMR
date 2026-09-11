---
roles: [editor]
status: active
reviewed: 2026-09-10
---
# Scene-v2 renderer handoff

The editor submits one ordered list of twelve approved scene outputs. Each item
contains an ordinal, title, video reference, and title-audio reference. Preserve
ordinal order and title spelling. Do not create, rename, or substitute assets.

Use the workflow-pinned render preset and voice profile. The deterministic
renderer validates them, stages media, joins saved presentation audio with the
title-only narration, adds the presentation label, and records final provenance.
Pass only the opaque preset reference; presentation labels and saved audio stay inside the renderer.

Use 30 fps, portrait 9:16, 202 frames per scene, and narration start frame 18.
Report unavailable preset, asset, duration, or renderer errors. Inspect a returned
job rather than submitting it again. A completed render is technically verified
only when the renderer reports a verified final artifact and provenance.
