---
roles: [producer]
status: active
reviewed: 2026-09-10
---
# Produce assets from the approved plan

The producer translates approved scenes into generation requests. Preserve each
scene ordinal, title and visual prompt. It does not revise the concept set,
choose publishing settings or edit the compilation.

## Current branches and visual direction

The legacy role tool is `sora_generate` in `src/myloware/tools/sora.py`; use
only its documented arguments when that branch is selected. The deterministic
scene-studio branch instead uses `studio/assets.py`, `providers/media.py`, and
`fal_voice_batch` under service control. This reference does not authorize an
agent to select a provider, submit media, or change those branches.

For every scene video, preserve one recognizable object, one visible impossible
action, a simple camera instruction, and a concise surreal material or setting.
Keep one continuous portrait shot. Exclude rendered words, captions, logos,
voiceover, music, foley, people, cuts, long plots, and stacked transformations.
Titles and narration remain separate metadata and title-only audio. These are
durable creative constraints, not provider model parameters or billing claims.

## Worker handoff

The target workflow persists twelve intended scene asset jobs, submits them all,
then tracks each independently. The provider controls active generation capacity.
Title-only narration preparation and music selection can overlap with queued video jobs.
The orchestration layer owns request IDs, retry limits, budget reservation,
status polling and completion collection.

Return only observed submission results. Missing, failed and pending outputs are
different states. Retry decisions belong to the workflow; do not silently submit
the full batch again when one clip fails.

## Evidence

Current contracts: `src/myloware/studio/assets.py`,
`src/myloware/providers/media.py`, `src/myloware/studio/voice_batch.py`, and
`src/myloware/tools/sora.py` for the separate legacy role-tool branch.
