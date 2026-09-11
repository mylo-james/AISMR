# Native TikTok local sandbox test

`aismr tiktok serve` provides an owner-operated connection and private Direct Post test for a previously approved local MP4. It is separate from the visitor studio publication adapter. Starting the command does not publish anything.

## Credentials and callback

The `--credentials` path must be an enabled native 1Password environment FIFO containing `AISMR_TIKTOK_CLIENT_KEY`, `AISMR_TIKTOK_CLIENT_SECRET`, and `OPENAI_API_KEY`. The OpenAI key is used for a fresh caption moderation check at publication. The command retains only these values in memory. Do not copy them into this document or a tracked environment file.

Two services bind to loopback:

- `127.0.0.1:8315`: authenticated owner review, local media preview, publishing and status controls.
- `127.0.0.1:8316`: callback service containing only health, authorization start and OAuth callback routes.

Configure an HTTPS route to the callback service and register its exact `/auth/tiktok/callback` URL in the TikTok sandbox Login Kit settings. The `--callback-origin` value is the HTTPS origin without a path. Keep the owner service private to the Mac. A temporary tunnel changes its hostname after restart and therefore requires updating the registered callback. A tunnel provider relays the OAuth authorization code and state; obtain authorization for that connection before exposing the callback.

## Run

Supply the approved artifact and its existing approval/moderation receipt. Use one durable receipt path for this posting attempt:

```sh
.venv/bin/python -m myloware.cli.main tiktok serve \
  --credentials .local/aismr/credentials.env \
  --artifact /absolute/path/to/approved-video.mp4 \
  --approval /absolute/path/to/approval-operation.json \
  --receipt /absolute/path/to/native-post-operation.json \
  --callback-origin https://your-registered-callback-host \
  --ready-file .local/aismr/tiktok-ready.json
```

The ready file is owner-readable and contains a one-use local bootstrap URL, not provider credentials. Open that URL locally to establish the owner session. Start TikTok authorization from the review screen. The connection requests `user.info.basic` and `video.publish`, verifies the expected account `aismr698`, and checks current creator capabilities.

Preview the MP4 and caption, choose Only me, confirm that the TikTok account is private, and provide the displayed music consent before submitting. Privacy has no default selection. Interaction options start unchecked and respect current creator restrictions. This bounded test supports noncommercial private posts and marks the generated video as AI content.

## Upload and recovery

The service validates the approved path, SHA-256, size, owner approval and existing sampled-media moderation evidence. At submission it rechecks the creator and caption, then copies the MP4 into an anonymous temporary file and verifies that copy before initialization. The same open copy supplies all upload chunks, so a later change to the original path cannot change the uploaded bytes. Allow temporary disk space equal to the MP4 size.

The receipt distinguishes intent from an initialization attempt. Once TikTok returns a publish ID, the service saves it before uploading bytes. Upload requests allow 300 seconds per chunk with a 20-second connection timeout. There are no automatic publication retries. Existing receipts prevent resubmission and a file lock prevents another process from sharing an active attempt.

If initialization or transfer has an uncertain outcome, retain the receipt and inspect native status when a publish ID exists. Do not delete the receipt or switch to a fresh receipt path to retry an uncertain attempt. A provider acceptance response alone does not establish a completed TikTok post. `PUBLISH_COMPLETE` establishes native completion; a playable URL is not fabricated from an incomplete response.

Tokens exist only in process memory. Restarting requires authorization again to inspect a saved attempt. Stopping the local process removes its ready file; stop any temporary tunnel process as well.

## Verification boundary

The focused HTTP transport, session and launcher tests cover the native API contract, browser-bound one-use OAuth state, account and consent checks, local/public route separation, duplicate prevention, uncertain outcomes, and exact uploaded bytes. Offline tests do not establish TikTok eligibility, live login success or video delivery.
