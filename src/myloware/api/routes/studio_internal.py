"""Run-scoped renderer media and signed callback ingestion."""

import hmac
import json
import re
from hashlib import sha256
from pathlib import Path
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse, JSONResponse
from sqlalchemy import select, update

from myloware.api.routes.studio import get_store
from myloware.config import settings
from myloware.storage.models import Artifact, Job, _utc_now
from myloware.storage.studio_models import StudioAsset, StudioEvent, StudioRun
from myloware.storage.studio_store import StudioError, StudioStore

router = APIRouter(prefix="/v1/studio", tags=["Studio renderer"])


_store_dependency = Depends(get_store)


def _error(code: str, status: int = 404):
    return JSONResponse({"error": code}, status_code=status)


async def _media_run(store: StudioStore, run_id: UUID, token: str):
    if not store.config.enabled or not hmac.compare_digest(token, store.sign("media", str(run_id))):
        return None
    async with store.factory() as session:
        run = await session.get(StudioRun, run_id)
        return run if run and run.expires_at > _utc_now() else None


@router.get("/internal/{run_id}/{token}/fixture/{name}")
async def fixture(run_id: UUID, token: str, name: str, store: StudioStore = _store_dependency):
    run = await _media_run(store, run_id, token)
    if (
        run is None
        or run.mode != "fixture"
        or not re.fullmatch(
            r"(?:month-(?:0[1-9]|1[0-2])\.(?:mp4|wav)|voice-batch(?:-timestamps)?\.(?:wav|json)|scene-(?:0[1-9]|1[0-2])\.mp4|scene-(?:0[1-9]|1[0-2])-title\.wav|title-batch(?:-timestamps)?\.(?:wav|json))",
            name,
        )
    ):
        return _error("asset_not_found")
    root = (store.config.fixture_root / str(run_id)).resolve()
    path = (root / name).resolve()
    if path.parent != root or not path.is_file():
        return _error("asset_not_found")
    return FileResponse(
        path,
        media_type=(
            "video/mp4"
            if name.endswith(".mp4")
            else "application/json" if name.endswith(".json") else "audio/wav"
        ),
        headers={"Cache-Control": "no-store"},
    )


@router.get("/internal/{run_id}/{token}/recorded/{kind}/{name}")
async def recorded(
    run_id: UUID, token: str, kind: str, name: str, store: StudioStore = _store_dependency
):
    from myloware.studio.recorded import RecordedMediaArchive, RecordedMediaError

    run = await _media_run(store, run_id, token)
    if run is None or run.mode != "recorded" or store.config.recorded_root is None:
        return _error("asset_not_found")
    try:
        archive = RecordedMediaArchive(store.config.recorded_root)
        if kind == "video" and re.fullmatch(r"(?:0[1-9]|1[0-2])-video\.mp4", name):
            selected = archive.video(int(name[:2]))
            mime = "video/mp4"
        elif kind == "voice" and name == "original-batch.mp3":
            selected, mime = archive.batch_voice(), "audio/mpeg"
        elif kind == "music" and name == "tender-moment.mp3":
            selected, mime = archive.music(), "audio/mpeg"
        else:
            return _error("asset_not_found")
    except (RecordedMediaError, OSError, ValueError):
        return _error("asset_not_found")
    return FileResponse(selected.path, media_type=mime, headers={"Cache-Control": "no-store"})


@router.get("/internal/{run_id}/{token}/local/{kind}/{name}")
async def local(
    run_id: UUID, token: str, kind: str, name: str, store: StudioStore = _store_dependency
):
    """Serve only hash-checked private replay inputs from the configured local archive."""
    from myloware.studio.local_scene_media import LocalSceneMediaArchive, LocalSceneMediaError

    run = await _media_run(store, run_id, token)
    if run is None or run.mode != "local" or store.config.local_media_root is None:
        return _error("asset_not_found")
    try:
        archive = LocalSceneMediaArchive(store.config.local_media_root)
        if kind == "video" and re.fullmatch(r"(?:0[1-9]|1[0-2])-video\.mp4", name):
            selected, mime = archive.video(int(name[:2])), "video/mp4"
        elif kind == "voice" and name == "title-batch.wav":
            selected, mime = archive.batch(), "audio/wav"
        elif kind == "voice" and re.fullmatch(r"(?:0[1-9]|1[0-2])-title\.wav", name):
            selected, mime = archive.title(int(name[:2])), "audio/wav"
        else:
            return _error("asset_not_found")
    except (LocalSceneMediaError, OSError, ValueError):
        return _error("asset_not_found")
    return FileResponse(selected.path, media_type=mime, headers={"Cache-Control": "no-store"})


@router.get("/internal/{run_id}/{token}/assets/{artifact_id}")
async def asset(
    run_id: UUID, token: str, artifact_id: UUID, store: StudioStore = _store_dependency
):
    run = await _media_run(store, run_id, token)
    if run is None:
        return _error("asset_not_found")
    async with store.factory() as session:
        artifact = await session.get(Artifact, artifact_id)
        selected = await session.scalar(
            select(StudioAsset).where(
                StudioAsset.run_id == run_id,
                StudioAsset.revision == run.revision,
                StudioAsset.artifact_id == artifact_id,
                StudioAsset.status == "ready",
            )
        )
    if artifact is None or artifact.run_id != run_id or selected is None or not artifact.uri:
        return _error("asset_not_found")
    root = (store.config.media_root / str(run_id)).resolve()
    path = Path(artifact.uri).resolve()
    if (
        root not in path.parents
        or not path.is_file()
        or sha256(path.read_bytes()).hexdigest() != selected.sha256
    ):
        return _error("asset_not_found")
    return FileResponse(
        path,
        media_type="video/mp4" if selected.kind == "video" else "audio/wav",
        headers={"Cache-Control": "no-store"},
    )


@router.get("/internal/{run_id}/{token}/music/{music_id}")
async def music(run_id: UUID, token: str, music_id: str, store: StudioStore = _store_dependency):
    from myloware.studio.music import cleared_music

    run = await _media_run(store, run_id, token)
    if run is None or music_id != store.config.music_id:
        return _error("asset_not_found")
    try:
        path = cleared_music(music_id)
    except (StudioError, OSError):
        return _error("asset_not_found")
    return FileResponse(path, media_type="audio/mpeg", headers={"Cache-Control": "no-store"})


@router.post("/callbacks/remotion/{run_id}")
async def callback(run_id: UUID, request: Request, store: StudioStore = _store_dependency):
    if not store.config.enabled:
        return _error("studio_disabled", 503)
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > 16384:
            return _error("callback_too_large", 413)
    secret = settings.remotion_webhook_secret
    if not secret:
        return _error("callback_auth_unconfigured", 503)
    expected = "sha512=" + hmac.new(secret.encode(), body, "sha512").hexdigest()
    if not hmac.compare_digest(request.headers.get("x-remotion-signature", ""), expected):
        return _error("callback_signature_invalid", 401)
    try:
        data = json.loads(body)
        if (
            not isinstance(data, dict)
            or data.get("run_id") != str(run_id)
            or data.get("status") not in {"done", "error"}
        ):
            return _error("callback_invalid", 422)
    except (ValueError, TypeError):
        return _error("callback_invalid", 422)
    async with store.transaction() as session:
        run = await session.get(StudioRun, run_id)
        if run is None or data.get("job_id") != run.render_job_id:
            return _error("callback_job_mismatch", 409)
        if not run.render_input_hash or data.get("input_hash") != run.render_input_hash:
            return _error("callback_input_hash_mismatch", 409)
        duplicate = await session.scalar(
            select(StudioEvent.id).where(
                StudioEvent.run_id == run_id,
                StudioEvent.event_type == "render_callback_received",
            )
        )
        if duplicate:
            return {"accepted": True, "duplicate": True}
        if run.status != "editing":
            return _error("callback_out_of_order", 409)
        await store.event(
            session,
            run,
            "editing",
            "render_callback_received",
            {"status": data["status"]},
        )
        # A signed callback wakes the real waiting graph. Its worker still polls
        # the run-bound inspector and verifies the final bytes before review.
        await session.execute(
            update(Job)
            .where(
                Job.job_type == "studio.advance",
                Job.run_id == run_id,
                Job.status == "pending",
            )
            .values(available_at=_utc_now())
        )
    return {"accepted": True, "duplicate": False}
