"""Owner-only native TikTok proof, with an isolated public OAuth callback app.

This is deliberately separate from the visitor runtime. Tokens stay in memory;
restarting requires owner authorization again. A durable one-attempt receipt
prevents restarting or double-clicking from creating a second post.
"""

from __future__ import annotations

import asyncio
import fcntl
import hashlib
import html
import json
import os
import re
import secrets
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response

from myloware.providers.tiktok_native import (
    CreatorInfo,
    NativeTikTokClient,
    TikTokApiError,
    TikTokInitializedUploadError,
    TikTokNativeError,
    TikTokToken,
)
from myloware.studio.moderation import Moderator


def file_sha256(path: Path) -> str:
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def write_private_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(path.name + "." + secrets.token_hex(8) + ".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w") as destination:
            json.dump(value, destination, indent=2)
            destination.flush()
            os.fsync(destination.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


@dataclass(frozen=True)
class ApprovedVideo:
    path: Path
    sha256: str
    size: int
    duration: float
    caption: str

    @classmethod
    def from_receipt(cls, path: Path, receipt_path: Path) -> ApprovedVideo:
        receipt = json.loads(receipt_path.read_text())
        approval = receipt.get("owner_approval", {})
        moderation = receipt.get("moderation", {})
        verdicts = moderation.get("verdicts", [])
        digest = file_sha256(path)
        if (
            not approval.get("statement")
            or Path(approval.get("approved_artifact", "")).resolve() != path.resolve()
            or receipt.get("sha256") != digest
            or receipt.get("bytes") != path.stat().st_size
            or not verdicts
            or any(verdict.get("safe") is not True for verdict in verdicts)
            or not moderation.get("frames")
        ):
            raise ValueError("The video needs its matching owner approval and moderation receipt")
        duration = float(receipt["duration_seconds"])
        if not 0 < duration <= 600:
            raise ValueError("The approved duration is invalid")
        return cls(
            path.resolve(), digest, path.stat().st_size, duration, str(receipt["config"]["caption"])
        )

    def verify(self) -> None:
        if self.path.stat().st_size != self.size or file_sha256(self.path) != self.sha256:
            raise ValueError("The reviewed video has changed")


class OwnerTikTokSession:
    def __init__(
        self,
        client: NativeTikTokClient,
        moderator: Moderator,
        video: ApprovedVideo,
        receipt_path: Path,
        callback_origin: str,
        *,
        owner_port: int = 8315,
        expected_username: str = "aismr698",
    ) -> None:
        parsed = urlsplit(callback_origin)
        if (
            parsed.scheme != "https"
            or not re.fullmatch(r"[a-zA-Z0-9.-]+", parsed.hostname or "")
            or parsed.username
            or parsed.password
            or parsed.port not in (None, 443)
            or parsed.path not in ("", "/")
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("Callback origin must be a plain HTTPS origin")
        self.client, self.moderator, self.video = client, moderator, video
        self.receipt_path = receipt_path
        self.callback_origin = callback_origin.rstrip("/")
        self.redirect_uri = self.callback_origin + "/auth/tiktok/callback"
        self.owner_origin = f"http://127.0.0.1:{owner_port}"
        self.expected_username = expected_username.lstrip("@").lower()
        self.owner_key = secrets.token_urlsafe(32)
        self.bootstrap = secrets.token_urlsafe(32)
        self.csrf = secrets.token_urlsafe(32)
        self.tickets: dict[str, float] = {}
        self.states: dict[str, tuple[str, float]] = {}
        self.token: TikTokToken | None = None
        self.token_deadline = 0.0
        self.creator: CreatorInfo | None = None
        self.lock = asyncio.Lock()
        self.busy = False
        self.tasks: set[asyncio.Task[None]] = set()
        self.message = "Connect the sandbox account to begin."
        self.last_status_check = 0.0
        receipt_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(str(receipt_path) + ".lock", os.O_CREAT | os.O_RDWR, 0o600)
        self._lock_file = os.fdopen(fd, "w")
        try:
            fcntl.flock(self._lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self._lock_file.close()
            raise ValueError("Another process owns this native posting receipt") from None
        self.receipt = json.loads(receipt_path.read_text()) if receipt_path.exists() else {}
        if self.receipt and self.receipt.get("sha256") != video.sha256:
            self.close()
            raise ValueError("Posting receipt belongs to another video")

    def close(self) -> None:
        self.token = None
        self._lock_file.close()

    def save(self, **changes: Any) -> None:
        self.receipt.update(changes, updated_at=time.time())
        write_private_json(self.receipt_path, self.receipt)

    def access_token(self) -> str:
        if self.token is None or time.monotonic() >= self.token_deadline:
            raise HTTPException(409, "Connect TikTok again before continuing")
        return self.token.access_token

    def validate_creator(self, creator: CreatorInfo) -> None:
        if creator.creator_username.lstrip("@").lower() != self.expected_username:
            raise HTTPException(409, "This connection is for a different TikTok account")
        if "SELF_ONLY" not in creator.privacy_level_options:
            raise HTTPException(
                409, "TikTok does not currently allow a private post for this account"
            )
        if self.video.duration > creator.max_video_post_duration_sec:
            raise HTTPException(409, "The video exceeds the connected account's duration limit")

    async def refresh_creator(self) -> CreatorInfo:
        creator = await self.client.creator_info(self.access_token())
        self.validate_creator(creator)
        self.creator = creator
        return creator

    async def upload(self, caption: str, settings: dict[str, bool]) -> None:
        try:
            # Anonymous snapshot: no pathname can be replaced after verification.
            with tempfile.TemporaryFile(mode="w+b", dir=self.receipt_path.parent) as snapshot:
                digest = hashlib.sha256()
                size = 0
                with self.video.path.open("rb") as source:
                    while chunk := source.read(1024 * 1024):
                        digest.update(chunk)
                        snapshot.write(chunk)
                        size += len(chunk)
                if size != self.video.size or digest.hexdigest() != self.video.sha256:
                    raise ValueError("The reviewed video has changed")
                snapshot.flush()
                snapshot.seek(0)
                self.save(state="initializing", init_attempts=1, provider_effect_confirmed=False)
                upload = await self.client.initialize_video(
                    self.access_token(),
                    self.video.size,
                    caption,
                    privacy_level="SELF_ONLY",
                    is_aigc=True,
                    **settings,
                )
                self.save(
                    state="uploading",
                    publish_id=upload.publish_id,
                    chunk_size=upload.chunk_size,
                    total_chunk_count=upload.total_chunk_count,
                    provider_effect_confirmed=True,
                )
                await self.client.upload_stream(upload, snapshot)
            self.save(state="processing", transfer_complete=True)
            self.message = "Video transferred. Check TikTok's processing status."
        except TikTokInitializedUploadError as exc:
            self.save(
                state="held",
                publish_id=exc.publish_id,
                provider_effect_confirmed=True,
                transfer_complete=False,
                error_type=type(exc).__name__,
                error_code=exc.reason_code,
                upload_host=exc.upload_host,
            )
            self.message = (
                "TikTok initialized the attempt, but its upload response could not be validated. "
                "No video data was sent. The publish ID is saved for status checks."
            )
        except (TikTokNativeError, httpx.HTTPError, OSError, ValueError, HTTPException) as exc:
            rejected = (
                isinstance(exc, TikTokApiError)
                and exc.code in {"invalid_param", "invalid_params"}
                and exc.status_code in {200, 400}
                and not self.receipt.get("publish_id")
            )
            self.save(
                state="rejected" if rejected else "unknown",
                error_type=type(exc).__name__,
                error_code=getattr(exc, "code", None),
                http_status=getattr(exc, "status_code", None),
                log_id=getattr(exc, "log_id", None),
            )
            self.message = (
                "TikTok rejected initialization. No video data was uploaded. This attempt is retained."
                if rejected
                else "The attempt did not finish clearly. It will not be retried automatically."
            )
        finally:
            self.busy = False


def _page(body: str) -> HTMLResponse:
    return HTMLResponse(
        """<!doctype html><html lang="en"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>AISMR · TikTok sandbox</title>
<style>body{margin:0;background:#e8e0ff;color:#10133d;font:17px/1.5 system-ui}main{max-width:760px;margin:32px auto;padding:24px;background:#fffdf6;border:3px solid #10133d;box-shadow:6px 6px #10133d}h1{font-family:monospace}button,select,textarea{font:inherit;padding:10px;border:2px solid #10133d}button{background:#ffe36a;cursor:pointer}textarea{box-sizing:border-box;width:100%;min-height:100px}label{display:block;margin:14px 0}video{display:block;width:240px;max-width:100%;max-height:430px;background:#10133d}a{color:inherit}code{overflow-wrap:anywhere}fieldset{margin:16px 0}small{display:block}button:disabled{opacity:.5}p.notice{padding:12px;background:#e8e0ff}*:focus-visible{outline:3px solid #00a8b5;outline-offset:3px}</style><main>"""
        + body
        + "</main></html>"
    )


def _secure(response: Response) -> Response:
    response.headers.update(
        {
            "Cache-Control": "no-store",
            "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff",
            "X-Frame-Options": "DENY",
            "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; media-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'",
        }
    )
    return response


def create_apps(session: OwnerTikTokSession) -> tuple[FastAPI, FastAPI]:
    owner = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    callback = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @owner.middleware("http")
    async def owner_gate(request: Request, call_next: Any) -> Response:
        if request.headers.get("host") != urlsplit(session.owner_origin).netloc:
            return _secure(Response(status_code=403))
        if not request.url.path.startswith("/connect/") and not secrets.compare_digest(
            request.cookies.get("aismr_owner", ""), session.owner_key
        ):
            return _secure(Response(status_code=403))
        if request.method == "POST" and request.headers.get("origin") != session.owner_origin:
            return _secure(Response(status_code=403))
        response = _secure(await call_next(request))
        # Native HTML form POSTs need their real Origin for the CSRF boundary.
        # no-referrer makes browsers send Origin: null; cross-origin referrers
        # are still suppressed by same-origin. Public callbacks stay no-referrer.
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Content-Security-Policy"] = response.headers[
            "Content-Security-Policy"
        ].replace(
            "form-action 'self'",
            "form-action 'self' " + session.callback_origin + " https://www.tiktok.com",
        )
        return response

    @callback.middleware("http")
    async def callback_gate(request: Request, call_next: Any) -> Response:
        if request.headers.get("host") != urlsplit(session.callback_origin).netloc:
            return _secure(Response(status_code=403))
        return _secure(await call_next(request))

    @owner.get("/connect/{ticket}")
    async def bootstrap(ticket: str) -> Response:
        if not session.bootstrap or not secrets.compare_digest(ticket, session.bootstrap):
            raise HTTPException(403)
        session.bootstrap = ""
        response = RedirectResponse("/", status_code=303)
        response.set_cookie("aismr_owner", session.owner_key, httponly=True, samesite="strict")
        return response

    def csrf_input() -> str:
        return f'<input type="hidden" name="csrf" value="{session.csrf}">'

    async def form(request: Request) -> dict[str, str]:
        body = bytearray()
        async for piece in request.stream():
            body.extend(piece)
            if len(body) > 12000:
                raise HTTPException(413)
        fields = parse_qs(body.decode("utf-8"), keep_blank_values=True)
        if any(len(values) != 1 for values in fields.values()):
            raise HTTPException(400, "Duplicate form fields")
        values = {key: value[0] for key, value in fields.items()}
        if not secrets.compare_digest(values.get("csrf", ""), session.csrf):
            raise HTTPException(403)
        return values

    @owner.get("/")
    async def dashboard() -> Response:
        body = "<p>AISMR / NATIVE TIKTOK TEST</p><h1>Connect, review, post.</h1>"
        body += f'<p class="notice">{html.escape(session.message)}</p>'
        body += f'<form method="post" action="/authorize">{csrf_input()}<button>Connect @{html.escape(session.expected_username)} with TikTok</button></form>'
        if session.token and not session.busy:
            try:
                creator = await session.refresh_creator()
            except (TikTokNativeError, httpx.HTTPError, ValueError, HTTPException):
                body += "<p>Creator details are unavailable. Reconnect before posting.</p>"
            else:
                body += f"<h2>{html.escape(creator.creator_nickname)} <small>@{html.escape(creator.creator_username)}</small></h2>"
                if not session.receipt:
                    body += '<video controls preload="metadata" src="/media"></video>'
                    body += f"<p>{session.video.duration:g} seconds · AI-generated video and narration · CC0 background music</p>"
                    body += f'<form method="post" action="/publish">{csrf_input()}'
                    body += f'<label>Caption<textarea name="caption" maxlength="2200" required>{html.escape(session.video.caption)}</textarea></label>'
                    body += '<label>Visibility<select name="privacy" required><option value="" selected disabled>Select visibility</option><option value="SELF_ONLY">Only me (sandbox)</option></select></label><fieldset><legend>Interactions</legend>'
                    for name, disabled in (
                        ("comment", creator.comment_disabled),
                        ("duet", creator.duet_disabled),
                        ("stitch", creator.stitch_disabled),
                    ):
                        body += f'<label><input type="checkbox" name="allow_{name}" value="yes" {"disabled" if disabled else ""}> Allow {name}</label>'
                    body += '</fieldset><label><input type="checkbox" name="promotes_brand" value="yes"> This video promotes a brand or business</label><small>This private sandbox test supports the reviewed noncommercial video. Commercial posts require a separate review.</small>'
                    body += '<label><input type="checkbox" name="private_account" value="yes" required> My TikTok account is set to private for this sandbox test.</label>'
                    body += '<label><input type="checkbox" name="consent" value="yes" required> Post this video with the settings above. By posting, I agree to TikTok’s <a href="https://www.tiktok.com/legal/page/global/music-usage-confirmation/en" target="_blank" rel="noreferrer">Music Usage Confirmation</a>.</label><button>Post privately to TikTok</button></form>'
        if session.receipt:
            body += f'<h2>Attempt: {html.escape(str(session.receipt.get("state", "unknown")))}</h2>'
            body += "<p>This attempt is retained. No automatic retry will occur.</p>"
            if session.receipt.get("error_code"):
                body += f'<p>TikTok error: <code>{html.escape(str(session.receipt["error_code"]))}</code></p>'
            if session.receipt.get("publish_id"):
                body += (
                    f'<p>Publish ID: <code>{html.escape(session.receipt["publish_id"])}</code></p>'
                )
                body += f'<form method="post" action="/status">{csrf_input()}<button {"disabled" if session.busy else ""}>Check TikTok status</button></form>'
            if session.receipt.get("native_status"):
                body += f'<p>TikTok status: <strong>{html.escape(session.receipt["native_status"])}</strong></p>'
            if session.receipt.get("fail_reason"):
                body += f'<p>{html.escape(session.receipt["fail_reason"])}</p>'
        return _page(body)

    @owner.get("/media")
    async def media() -> Response:
        return FileResponse(session.video.path, media_type="video/mp4")

    @owner.get("/state")
    async def state() -> Response:
        return JSONResponse(
            {
                "connected": session.token is not None,
                "username": session.creator.creator_username if session.creator else None,
                "busy": session.busy,
                "receipt": session.receipt,
                "message": session.message,
            }
        )

    @owner.post("/authorize")
    async def authorize(request: Request) -> Response:
        await form(request)
        if session.busy:
            raise HTTPException(409, "An upload is already in progress")
        ticket = secrets.token_urlsafe(32)
        session.tickets = {ticket: time.monotonic() + 600}
        return RedirectResponse(
            session.callback_origin + "/auth/tiktok/start?ticket=" + ticket, status_code=303
        )

    @callback.get("/health")
    async def health() -> Response:
        return JSONResponse({"service": "aismr-tiktok-callback", "ready": True})

    @callback.get("/auth/tiktok/start")
    async def oauth_start(request: Request) -> Response:
        ticket = request.query_params.get("ticket", "")
        if session.tickets.pop(ticket, 0) < time.monotonic():
            raise HTTPException(403, "Start the connection from AISMR")
        state_key, browser_key = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        session.states = {state_key: (browser_key, time.monotonic() + 600)}
        response = RedirectResponse(
            session.client.authorization_url(session.redirect_uri, state_key), status_code=303
        )
        response.set_cookie(
            "aismr_oauth",
            browser_key,
            secure=True,
            httponly=True,
            samesite="lax",
            max_age=600,
            path="/auth/tiktok/",
        )
        return response

    @callback.get("/auth/tiktok/callback")
    async def oauth_callback(request: Request) -> Response:
        fields = request.query_params
        state_key = fields.get("state", "")
        pending = session.states.get(state_key)
        if (
            not pending
            or pending[1] < time.monotonic()
            or not secrets.compare_digest(pending[0], request.cookies.get("aismr_oauth", ""))
            or any(len(fields.getlist(key)) != 1 for key in ("state", "code"))
        ):
            raise HTTPException(
                403, "This login response did not match the browser that started it"
            )
        session.states.pop(state_key)
        if fields.get("error") or not fields.get("code") or len(fields["code"]) > 4096:
            raise HTTPException(400, "TikTok authorization was not granted")
        try:
            token = await session.client.exchange_code(fields["code"], session.redirect_uri)
            if not {"user.info.basic", "video.publish"}.issubset(token.scope):
                raise ValueError("Required TikTok permissions were not granted")
            creator = await session.client.creator_info(token.access_token)
            session.validate_creator(creator)
            identity = hashlib.sha256(token.open_id.encode()).hexdigest()
            if session.receipt and session.receipt.get("account_binding") != identity:
                raise ValueError("The attempt belongs to another TikTok authorization")
            session.token, session.creator = token, creator
            session.token_deadline = time.monotonic() + max(0, token.expires_in - 30)
            session.message = "TikTok connected. Review the video and select the posting settings."
        except (TikTokNativeError, httpx.HTTPError, ValueError, HTTPException):
            session.message = "TikTok connection was not completed for the configured account."
            raise HTTPException(
                400, "TikTok connection failed. Return to AISMR to retry authorization."
            ) from None
        response = _page(
            "<h1>TikTok connected.</h1><p>Return to the AISMR tab on your Mac and refresh it to review the private post.</p>"
        )
        response.delete_cookie("aismr_oauth", path="/auth/tiktok/")
        return response

    @owner.post("/publish")
    async def publish(request: Request) -> Response:
        values = await form(request)
        async with session.lock:
            if session.receipt or session.busy:
                raise HTTPException(409, "This video already has a recorded native posting attempt")
            if (
                values.get("privacy") != "SELF_ONLY"
                or values.get("consent") != "yes"
                or values.get("private_account") != "yes"
                or values.get("promotes_brand")
            ):
                raise HTTPException(
                    400,
                    "Select private visibility and confirm the noncommercial private-account test",
                )
            caption = values.get("caption", "").strip()
            if not caption or len(caption) > 2200:
                raise HTTPException(400, "Caption must contain 1 to 2200 characters")
            creator = await session.refresh_creator()
            for name in ("comment", "duet", "stitch"):
                if values.get("allow_" + name) and getattr(creator, name + "_disabled"):
                    raise HTTPException(400, "An interaction is disabled by the TikTok account")
            try:
                session.video.verify()
            except (ValueError, OSError):
                raise HTTPException(409, "The reviewed video has changed") from None
            verdict = await session.moderator.moderate_text(caption, stage="native_tiktok_caption")
            if not verdict.safe:
                raise HTTPException(400, "Caption did not pass moderation")
            settings = {
                "disable_" + name: values.get("allow_" + name) != "yes"
                for name in ("comment", "duet", "stitch")
            }
            if session.token is None:
                raise HTTPException(401, "TikTok authentication is required")
            session.save(
                state="intent_recorded",
                operation_id=secrets.token_hex(16),
                sha256=session.video.sha256,
                bytes=session.video.size,
                account_binding=hashlib.sha256(session.token.open_id.encode()).hexdigest(),
                account=session.expected_username,
                caption=caption,
                privacy="SELF_ONLY",
                is_aigc=True,
                interactions=settings,
                owner_consent_at=time.time(),
                caption_moderation={"safe": True, "model": verdict.model},
                init_attempts=0,
                provider_effect_confirmed=False,
                automatic_retries=0,
            )
            session.busy = True
            task = asyncio.create_task(session.upload(caption, settings))
            session.tasks.add(task)
            task.add_done_callback(session.tasks.discard)
        return RedirectResponse("/", status_code=303)

    @owner.post("/status")
    async def status(request: Request) -> Response:
        await form(request)
        if session.busy or not session.receipt.get("publish_id"):
            raise HTTPException(409, "No completed transfer is available to check")
        if time.monotonic() - session.last_status_check < 5:
            return RedirectResponse("/", status_code=303)
        session.last_status_check = time.monotonic()
        native = await session.client.status(session.access_token(), session.receipt["publish_id"])
        stage = {"PUBLISH_COMPLETE": "published", "FAILED": "failed"}.get(
            native.status, "processing"
        )
        session.save(
            state=stage,
            native_status=native.status,
            fail_reason=native.fail_reason,
            public_post_ids=list(native.public_post_ids),
            uploaded_bytes=native.uploaded_bytes,
        )
        session.message = (
            "TikTok confirms the private post is complete. Open the connected account to watch it."
            if stage == "published"
            else "TikTok status updated."
        )
        return RedirectResponse("/", status_code=303)

    return owner, callback
