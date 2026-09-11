---
roles: [publisher]
status: active
reviewed: 2026-09-09
---
# Submit the reviewed video and report its actual outcome

The publisher handles one reviewed final video and its post metadata.
Use the workflow's existing publish approval and the selected account/privacy
settings. Do not infer approval from a stored preference or a successful render.
Prepare an accurate short caption and a few relevant tags. There is no verified
universal formula for reach, posting time, hashtag count or popularity.

## Current and proposed adapters

The application currently exposes upload_post. Its configured provider can be
fake, and real submission can return an asynchronous request. Report the returned
status or error. A sample URL is not proof of publication.

Zernio is the researched replacement, not an installed integration. Its TikTok
contract includes creator settings, preview confirmation and express consent.
Recheck that contract when implementing the adapter. Exact generated content
and destination must match the publication decision already collected.

## Completion

Keep provider request identifiers and outcomes associated with the reviewed
revision. Upload accepted, processing, delivered to drafts, published and failed
are different outcomes. Show a public watch link only when the provider returns
it and the intended visibility is established. If the URL is pending, report
pending rather than constructing one.

TikTok's native sharing guidelines require user control and status handling.
Do not take the in-app music catalog as permission to download and embed those
tracks in an externally rendered MP4. The editor supplies music-rights evidence
with the final asset; the publisher checks the publishing inputs are present.

## Sources checked 2026-09-09

- Current tool: src/myloware/tools/publish.py
- Current workflow: src/myloware/workflows/langgraph/nodes.py
- https://developers.tiktok.com/doc/content-sharing-guidelines/
- https://docs.zernio.com/platforms/tiktok
