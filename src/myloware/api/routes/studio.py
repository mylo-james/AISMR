"""Scoped visitor API for the AISMR monthly studio."""

from __future__ import annotations

import asyncio
import hmac
from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator

from myloware.api.verified_video import open_verified_file, open_verified_video
from myloware.config.studio import StudioSettings, get_studio_settings
from myloware.storage.database import get_async_session_factory
from myloware.storage.studio_store import StudioError, StudioStore
from myloware.studio.library_service import StudioLibraryService
from myloware.studio.planning_store import PlanRevisionRequest
from myloware.studio.portfolio_runtime import open_portfolio_library
from myloware.workflows.monthly import MONTHLY_CATALOG

router = APIRouter(prefix="/v1/studio", tags=["Studio"])


class SessionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RunBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    item: str = Field(min_length=1, max_length=48)
    request_key: str = Field(min_length=8, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")


class DecisionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    gate: Literal["ideas", "final"]
    decision: Literal["approve", "cancel", "revise", "keep_reviewing"]
    revision: StrictInt = Field(ge=1, le=3)
    subject_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    request_key: str = Field(min_length=8, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    publish_to_gallery: bool = False
    visibility_version: Literal["recent-creations-v1"] | None = None
    revision_feedback: PlanRevisionRequest | None = None

    @model_validator(mode="after")
    def feedback_requires_revision(self) -> DecisionBody:
        if self.revision_feedback is not None and (
            self.gate != "ideas" or self.decision != "revise"
        ):
            raise ValueError("revision feedback requires an idea revision")
        return self


_settings_dependency = Depends(get_studio_settings)


def get_store(config: StudioSettings = _settings_dependency) -> StudioStore:
    return StudioStore(get_async_session_factory(), config)


_store_dependency = Depends(get_store)


def _session_cookie(request: Request, store: StudioStore = _store_dependency) -> str | None:
    """Read only this origin's session name; never borrow another port's cookie."""
    name = getattr(store.config, "session_cookie_name", "aismr_session")
    return request.cookies.get(name)


_cookie_dependency = Depends(_session_cookie)


def _error(code: str, status: int) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": code})


async def _require_enabled(store: StudioStore) -> JSONResponse | None:
    if not store.config.enabled:
        return _error("studio_disabled", 503)
    if store.config.mode == "live":
        readiness = getattr(store.config, "live_configuration_errors", None)
        if not callable(readiness) or readiness():
            return _error("live_runtime_not_ready", 503)
    if store.config.mode == "planning" and store.config.planning_configuration_errors():
        return _error("planning_runtime_not_ready", 503)
    if store.config.mode == "local" and store.config.local_configuration_errors():
        return _error("local_runtime_not_ready", 503)
    return None


def _same_origin(request: Request, store: StudioStore) -> bool:
    if request.headers.get("origin") != store.config.origin:
        return False
    site = request.headers.get("sec-fetch-site")
    return site is None or site == "same-origin"


async def _visitor(store: StudioStore, cookie: str | None):
    return await store.visitor(cookie)


async def _mutation_visitor(request: Request, store: StudioStore, cookie: str | None):
    if not _same_origin(request, store):
        raise StudioError("origin_required", 403)
    visitor = await _visitor(store, cookie)
    supplied = request.headers.get("x-csrf-token", "")
    if not hmac.compare_digest(supplied, store.csrf(visitor.id)):
        raise StudioError("csrf_required", 403)
    return visitor


@router.get("/config")
async def config(store: StudioStore = _store_dependency):
    from myloware.studio.budget import persistent_admission_paused

    if store.config.mode == "planning":
        runtime_errors = store.config.planning_configuration_errors()
    elif store.config.mode == "local":
        runtime_errors = store.config.local_configuration_errors()
    else:
        runtime_errors = store.config.live_configuration_errors()
    paused = store.config.admission_paused
    if store.config.enabled:
        async with store.factory() as session:
            paused = paused or await persistent_admission_paused(session)
    return {
        "enabled": bool(store.config.enabled),
        "mode": store.config.mode,
        "planning_only": store.config.mode == "planning",
        "local_private": store.config.mode == "local",
        "live_ready": not runtime_errors,
        "generation": {
            "available": bool(
                store.config.enabled
                and not runtime_errors
                and not paused
                and store.config.mode != "recorded"
            ),
            "reason": (
                "admissions_paused"
                if paused
                else (
                    runtime_errors[0]
                    if runtime_errors
                    else (
                        "recorded_mode"
                        if store.config.mode == "recorded"
                        else "studio_disabled" if not store.config.enabled else None
                    )
                )
            ),
        },
        "suggestions": [item.model_dump() for item in MONTHLY_CATALOG.values()],
        "limits": {
            "fixture_unlimited": bool(
                store.config.mode == "fixture" and store.config.fixture_unlimited_admissions
            ),
            "visitor_runs_24h": store.config.visitor_runs_24h,
            "active_runs": store.config.active_runs,
            "plan_revisions": store.config.plan_revisions,
        },
        "recorded_item": "Teacup" if store.config.mode == "recorded" else None,
        "recorded_final_reuse": store.config.recorded_final_reuse,
    }


@router.get("/gallery")
async def gallery(store: StudioStore = _store_dependency):
    if store.config.recorded_final_reuse:
        return {"items": []}
    if store.config.mode in {"planning", "local"}:
        return {"items": []}
    try:
        async with open_portfolio_library(store) as library:
            items: list[dict[str, Any]] = [
                {
                    "id": str(entry.id),
                    "item": entry.item_label,
                    "mode": entry.source_mode,
                    "sha256": entry.final_sha256,
                    "created_at": entry.accepted_at.isoformat() + "Z",
                    "history": entry.history,
                    "url": f"/v1/studio/gallery/{entry.id}/video",
                }
                for entry in await library.gallery()
            ]
            if not items and store.config.mode == "recorded" and _loopback(store.config.origin):
                items = [
                    {**item, "legacy_local_recorded": True}
                    for item in await StudioLibraryService(store).gallery()
                ]
            return {"items": items}
    except StudioError as exc:
        if store.config.mode == "recorded" and _loopback(store.config.origin):
            return {
                "items": [
                    {**item, "legacy_local_recorded": True}
                    for item in await StudioLibraryService(store).gallery()
                ]
            }
        return _error(exc.code, exc.status)


@router.get("/gallery/{run_id}/video")
async def gallery_video(run_id: UUID, store: StudioStore = _store_dependency):
    if store.config.recorded_final_reuse:
        return _error("gallery_unavailable", 404)
    if store.config.mode in {"planning", "local"}:
        return _error("gallery_video_unavailable", 404)
    try:
        async with open_portfolio_library(store) as library:
            async with library.factory() as session:
                from myloware.storage.studio_models import StudioGalleryEntry

                entry = await session.get(StudioGalleryEntry, run_id)
            if entry is None:
                if store.config.mode == "recorded" and _loopback(store.config.origin):
                    legacy = await StudioLibraryService(store).gallery()
                    selected = next((item for item in legacy if item["id"] == str(run_id)), None)
                    if selected is not None:
                        return await asyncio.to_thread(
                            open_verified_video,
                            store.config.media_root.resolve(),
                            run_id,
                            selected["sha256"],
                        )
                return _error("gallery_video_unavailable", 404)
            path = await library.serving_path(run_id)
            if path is None:
                return _error("gallery_video_unavailable", 404)
            return await asyncio.to_thread(open_verified_file, path, entry.final_sha256)
    except (OSError, StudioError, ValueError):
        if store.config.mode == "recorded" and _loopback(store.config.origin):
            legacy = await StudioLibraryService(store).gallery()
            selected = next((item for item in legacy if item["id"] == str(run_id)), None)
            if selected is not None:
                try:
                    return await asyncio.to_thread(
                        open_verified_video,
                        store.config.media_root.resolve(),
                        run_id,
                        selected["sha256"],
                    )
                except (OSError, ValueError):
                    pass
        return _error("gallery_video_unavailable", 404)


def _loopback(origin: str) -> bool:
    from urllib.parse import urlsplit

    return urlsplit(origin).hostname in {"localhost", "127.0.0.1", "::1"}


@router.post("/session")
async def create_session(
    request: Request,
    _body: SessionBody,
    store: StudioStore = _store_dependency,
    cookie: str | None = _cookie_dependency,
):
    disabled = await _require_enabled(store)
    if disabled:
        return disabled
    if not _same_origin(request, store):
        return _error("origin_required", 403)
    if cookie:
        try:
            existing = await store.visitor(cookie)
            return {
                "visitor_id": existing.id,
                "csrf": store.csrf(existing.id),
                "latest_run_id": await store.latest_run(existing.id),
            }
        except StudioError:
            pass
    visitor, cookie = await store.new_visitor(request.client.host if request.client else "")
    response = JSONResponse(
        {
            "visitor_id": visitor.id,
            "csrf": store.csrf(visitor.id),
            "latest_run_id": None,
        }
    )
    response.set_cookie(
        store.config.session_cookie_name,
        cookie,
        httponly=True,
        secure=store.config.cookie_secure,
        samesite="strict",
        max_age=store.config.session_hours * 3600,
    )
    return response


@router.get("/session")
async def session(
    cookie: str | None = _cookie_dependency,
    store: StudioStore = _store_dependency,
):
    disabled = await _require_enabled(store)
    if disabled:
        return disabled
    try:
        visitor = await _visitor(store, cookie)
        return {
            "visitor_id": visitor.id,
            "csrf": store.csrf(visitor.id),
            "latest_run_id": await store.latest_run(visitor.id),
        }
    except StudioError as exc:
        return _error(exc.code, exc.status)


async def _signal_recorded(store: StudioStore, run_id: UUID) -> None:
    if not store.config.recorded_final_reuse:
        return
    from httpx import HTTPError
    from sqlalchemy.exc import SQLAlchemyError
    from vercel.queue import QueueError

    from myloware.observability.logging import get_logger
    from myloware.studio.hosted_queue import signal_run

    try:
        await signal_run(store, run_id)
    except (QueueError, HTTPError, SQLAlchemyError):
        # The SQL job is already committed. An authorized poll retries delivery.
        get_logger(__name__).warning("recorded_queue_signal_failed", run_id=str(run_id))


@router.post("/runs")
async def create_run(
    request: Request,
    body: RunBody,
    cookie: str | None = _cookie_dependency,
    store: StudioStore = _store_dependency,
):
    disabled = await _require_enabled(store)
    if disabled:
        return disabled
    try:
        visitor = await _mutation_visitor(request, store, cookie)
        run_id = await store.admit(
            visitor_id=visitor.id,
            item_text=body.item,
            start_key=body.request_key,
            ip=request.client.host if request.client else "",
        )
        snapshot = await store.snapshot(run_id, visitor_id=visitor.id)
        await _signal_recorded(store, run_id)
        return snapshot
    except StudioError as exc:
        return _error(exc.code, exc.status)
    except ValueError:
        return _error("invalid_item", 422)


@router.get("/runs/{run_id}")
async def get_run(
    run_id: UUID,
    share: str | None = None,
    cookie: str | None = _cookie_dependency,
    store: StudioStore = _store_dependency,
):
    try:
        visitor_id = None
        if cookie:
            try:
                visitor_id = (await _visitor(store, cookie)).id
            except StudioError:
                if not share:
                    raise
        snapshot = await store.snapshot(run_id, visitor_id=visitor_id, share=share)
        if visitor_id is not None and not share:
            await _signal_recorded(store, run_id)
        return snapshot
    except StudioError as exc:
        return _error(exc.code, exc.status)


@router.post("/runs/{run_id}/decisions")
async def decide(
    run_id: UUID,
    request: Request,
    body: DecisionBody,
    cookie: str | None = _cookie_dependency,
    store: StudioStore = _store_dependency,
):
    disabled = await _require_enabled(store)
    if disabled:
        return disabled
    try:
        visitor = await _mutation_visitor(request, store, cookie)
        if body.gate == "final" and body.decision == "keep_reviewing":
            # Declining public sharing is not an acceptance or cancellation.
            # Keep the existing private review intact and write no decision row.
            return await store.snapshot(run_id, visitor_id=visitor.id)
        await store.decide(
            run_id=run_id,
            visitor_id=visitor.id,
            gate=body.gate,
            decision=body.decision,
            revision=body.revision,
            subject_hash=body.subject_hash,
            decision_key=body.request_key,
            publish_to_gallery=body.publish_to_gallery,
            visibility_version=body.visibility_version,
            revision_feedback=(
                body.revision_feedback.model_dump() if body.revision_feedback is not None else None
            ),
        )
        snapshot = await store.snapshot(run_id, visitor_id=visitor.id)
        await _signal_recorded(store, run_id)
        return snapshot
    except StudioError as exc:
        return _error(exc.code, exc.status)


@router.get("/runs/{run_id}/preview")
async def preview(
    run_id: UUID,
    share: str | None = None,
    cookie: str | None = _cookie_dependency,
    store: StudioStore = _store_dependency,
):
    try:
        visitor_id = None
        if cookie:
            try:
                visitor_id = (await _visitor(store, cookie)).id
            except StudioError:
                if not share:
                    raise
        snapshot = await store.snapshot(run_id, visitor_id=visitor_id, share=share)
        final = snapshot.get("final")
        metadata = final.get("metadata") if isinstance(final, dict) else None
        expected_hash = final.get("sha256") if isinstance(final, dict) else None
        if (
            not isinstance(metadata, dict)
            or metadata.get("media_verified") is not True
            or not isinstance(expected_hash, str)
        ):
            return _error("preview_unavailable", 404)
        if snapshot.get("recorded_final_reuse"):
            from myloware.studio.hosted_recorded import recorded_preview_url

            run = await store.get_run(run_id)
            stored = run.final_metadata or {}
            if (
                not store.config.recorded_final_reuse
                or stored.get("bundle_sha256") != store.config.recorded_bundle_sha256
                or stored.get("recorded_final_reuse") is not True
            ):
                return _error("preview_unavailable", 404)
            url = await recorded_preview_url(
                store.config, expected_hash, stored.get("object_etag", "")
            )
            return RedirectResponse(
                url,
                status_code=307,
                headers={"Cache-Control": "private, no-store", "Referrer-Policy": "no-referrer"},
            )
        # A successfully acknowledged public projection remains the only valid
        # owner playback mapping after bounded source-media cleanup.
        try:
            async with open_portfolio_library(store) as library:
                source_key = f"{snapshot['mode']}:{run_id}:{snapshot['revision']}:{expected_hash}"
                entry = await library.entry_for_source(source_key)
                if entry is not None:
                    path = await library.serving_path(entry.id)
                    if path is not None:
                        return await asyncio.to_thread(open_verified_file, path, expected_hash)
        except StudioError:
            pass
        # A completed projection owns subsequent playback. Never resurrect an
        # expired or revoked public copy through leftover source bytes.
        from sqlalchemy import select

        from myloware.storage.studio_models import StudioGalleryProjectionIntent

        async with store.factory() as session:
            projected = await session.scalar(
                select(StudioGalleryProjectionIntent.id).where(
                    StudioGalleryProjectionIntent.run_id == run_id,
                    StudioGalleryProjectionIntent.revision == snapshot["revision"],
                    StudioGalleryProjectionIntent.final_hash == expected_hash,
                    StudioGalleryProjectionIntent.state == "complete",
                )
            )
        if projected is not None:
            return _error("preview_unavailable", 404)
        try:
            return await asyncio.to_thread(
                open_verified_video, store.config.media_root.resolve(), run_id, expected_hash
            )
        except (OSError, ValueError):
            return _error("preview_unavailable", 404)
    except StudioError as exc:
        return _error(exc.code, exc.status)
