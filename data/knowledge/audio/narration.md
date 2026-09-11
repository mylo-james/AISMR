---
roles: []
status: legacy
reviewed: 2026-09-09
---
# Batch narration with quiet-gap splitting

The selected and locally tested narration path is one fal-hosted Eleven V3
request containing all twelve title-only lines. The owner approved voice
`KmnvDXRA0HU55Q0aqkPG` and its cadence after the live audition. Pass that voice
explicitly; do not select a random voice or silently use a provider default.
This media proof does not establish that the durable workflow has migrated
from its existing per-line fallback. Keep the exact source text, the stable line
order and the selected voice with the monthly run record. Do not submit one
generation per line merely to create cadence: the editor creates the per-line
silence after splitting the returned batch audio.

The owner now prefers the natural delivery of the selected voice. Future
batches retain the pinned `[whispers]` delivery cue and title-only text, omitting the
final pause. Keep historical tag parsing for old receipts. One leading whisper
tag did not establish consistent whispering throughout the approved audition.
Do not add per-line whisper cues unless that delivery is requested again.

## Selected hosted path

`fal-ai/elevenlabs/tts/eleven-v3` accepts `text`, `voice`, `stability`,
`timestamps`, `language_code` and `apply_text_normalization` for this workflow.
Set `timestamps: true`. Although the endpoint description calls these word
timings, the verified live response was a list of character-alignment chunks.
Each chunk contains `characters`, `character_start_times_seconds` and
`character_end_times_seconds`; times are global and chunk edges can split
words or delivery tags. Flatten and validate the complete text against the
exact submitted text before deriving words. The local
`normalize_fal_timestamps(raw, input_text)` adapter implements this contract.
Do not treat arbitrary chunk boundaries as scene or word boundaries.

After the one audio file has downloaded, validate that every returned timing is
usable and match its text to the recorded lines in order. Provider alignment
is approximate: the initial timestamp-based split cut about 113 ms into October.
Use `detect_silences` and `plan_pause_splits` to select actual quiet gaps between
lines, keeping 300 ms of quiet padding. The current local detector uses -50 dB
for at least 250 ms. Missing or ambiguous gaps require review, never a fallback
cut through a word. Preserve the full first and last utterances. Sound aligned
to a delivery tag is not proof that the tag was spoken. Add scene spacing locally. Preserve the original audio and timestamp receipt. A missing,
empty or unmatched timestamp response is an unknown result, not permission to
generate the batch again.

fal lists Eleven V3 at $0.10 per 1,000 input characters with no minimums. Keep
the exact input text and the provider receipt so actual billing can be compared
with the estimate. The accepted live audition used 457 input characters and returned
`x-fal-billable-units: 0.457`, consistent with $0.0457 at that rate. This is a
provider-unit receipt, not an account billing reconciliation. Eleven V3 does not expose a `speed` field in this endpoint
schema. Cadence comes from punctuation, wording, timing-based splitting and
local silence, not a speed parameter.

## Kokoro fallback

The official Python implementation uses KPipeline, a language code and a voice;
it yields audio that can be written to a WAV file. The main Kokoro-82M weight
file is about 327 MB, with additional dependencies and voices needed.
Kokoro 0.9.4 declares Python >=3.10,<3.13. Use a separate compatible environment
for this Python 3.13 app rather than forcing an unsupported installation.
Installation footprint and performance must be measured on the target host.

Hosted Kokoro at `fal-ai/kokoro/american-english` accepts `prompt`, `voice` and
`speed`, and returns `audio.url`. It has no documented whisper instruction or
word-timestamp control. Published pricing is $0.02 per 1,000 characters. fal's
official text-to-speech deployment example calculates billable units as
`max(1, len(prompt) // 1000)` and describes a one-unit minimum. That is
evidence about a documented deployment pattern, not proof of this hosted
endpoint's production billing code. It matches the observed $0.02 charge for
each short request, so treat a $0.02 minimum as a strong inference and confirm
from the receipt before relying on it. Use Kokoro only when a normal,
separate-line fallback is approved. Hosted voice shares the account's request
capacity with video. Local voice uses the local runtime instead.

## Asset handoff

The editor receives twelve measured title narration files in scene ordinal order. The renderer
alone joins each title to its approved saved presentation audio; no caller supplies
calendar labels or creates replacement month speech. Whether they
were produced by batch splitting or an approved fallback. Keep the speech dry
and intelligible. Music selection, volume automation, placement and fades belong
to editing. Speech generation does not establish rights to a separate music track.

## Sources

- https://pypi.org/project/kokoro/
- https://github.com/hexgrad/kokoro
- https://huggingface.co/hexgrad/Kokoro-82M/tree/main
- https://fal.ai/models/fal-ai/kokoro/american-english/api
- https://fal.ai/models/fal-ai/kokoro/american-english
- https://fal.ai/docs/examples/audio-speech/deploy-text-to-speech-model
- https://fal.ai/models/fal-ai/elevenlabs/tts/eleven-v3
- https://fal.ai/elevenlabs
