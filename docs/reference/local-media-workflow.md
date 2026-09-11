# Local media workflow

Local mode tests the full AISMR workflow without requesting new video or speech. The creative agents use the normal Codex planner and actual content moderation. The provider adapters return saved Teacup footage and title narration, followed by a real local Remotion render.

## Execution

1. Generate twelve scene ideas and storyboards with the real explorers, curator and writer.
2. Review the storyboards. Approval remains bound to the exact plan revision.
3. Load twelve saved videos and the saved title-only narration batch from the local archive.
4. Verify the archive, media and narration splits.
5. Hand the ordered scenes to the renderer. The renderer adds the saved January through December recordings and labels.
6. Render locally and check the finished file.
7. Review and accept the private result. Local results cannot enter the public library.

The media is an ordinal substitute for provider output. A new scene titled “Shifting Teacup” can therefore receive a saved recording saying “Frozen Teacup.” The archive and asset receipts distinguish requested text from actual source text. This mode tests orchestration, collection, assembly and review. It does not establish how well a video provider follows the new storyboards.

## Configuration

Set `AISMR_MODE=local`, `AISMR_PLANNER_VERSION=creative-v2`, `AISMR_IDEATION_BACKEND=codex`, and `AISMR_LOCAL_MEDIA_ROOT` to the verified local archive. Enable the application and real renderer; disable live effects and public runtime. The private API can use loopback or secure Tailnet; the Codex worker must use loopback. Keep Fal and publishing credentials empty.

Use the authenticated local renderer with one job and one frame worker. Provider media fetches in this mode use run-scoped, signed local URLs. Live provider fetch policies retain their existing restrictions.

The saved-media archive identity is pinned when a local run is admitted. Replacing a bundle does not change the media of an existing run. Planning-only runs retain their original mode and finish at **Keep ideas**, including when viewed or continued under a local runtime. Preserve their database and session secret across restarts.

Text-agent and moderation usage still occurs. A zero media-generation reservation does not claim those services are free.
