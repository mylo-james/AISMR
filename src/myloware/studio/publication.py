"""Durable, approval-bound publication of a verified monthly final."""

from __future__ import annotations

from hashlib import sha256
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlparse
from uuid import UUID

from sqlalchemy import select

from myloware.providers.publishing import (
    PublicationResult,
    PublicationUnknown,
    PublishRejected,
)
from myloware.storage.models import _utc_now
from myloware.storage.studio_models import StudioAsset, StudioDecision, StudioRun
from myloware.storage.studio_store import StudioStore
from myloware.studio.moderation import ModerationVerdict, Moderator


class PublicationProvider(Protocol):
    async def submit(
        self,
        *,
        video_url: str,
        account_id: str,
        caption: str,
        privacy: str,
        ai_disclosure: bool,
        operation_key: str,
    ) -> PublicationResult: ...

    async def poll(self, post_id: str) -> PublicationResult: ...

    async def creator_capabilities(self, account_id: str) -> Any: ...


class PublicationTransfer(Protocol):
    """Create a short-lived public HTTPS URL for exactly one reviewed file.

    A live composition root may implement this using Zernio's documented media
    presign flow.  The service deliberately owns no HTTP client or credentials.
    """

    async def transfer(self, *, path: Path, sha256: str, operation_key: str) -> str: ...


class MonthlyPublication:
    """Own the durable boundary between final review and one TikTok post.

    Network effects happen after an intent commits.  A missing receipt or a
    transport error is retained as an unknown state and is never retried by this
    class, because a post might already exist.
    """

    def __init__(
        self,
        store: StudioStore,
        provider: PublicationProvider,
        moderator: Moderator,
        transfer: PublicationTransfer | None = None,
    ) -> None:
        self.store = store
        self.provider = provider
        self.moderator = moderator
        self.transfer = transfer

    async def publish(self, run_id: UUID) -> None:
        """Create one durable intent, then transfer and submit the reviewed bytes."""
        prepared = await self._prepare(run_id)
        if prepared is None:
            current = await self.store.get_run(run_id)
            if current.status == "publishing" and current.publish_state is None:
                await self._hold(run_id, "publication_preflight_failed")
            return
        run_id, operation_key, path, expected_hash, config, mode = prepared
        if mode == "fixture":
            await self._complete_fixture(run_id)
            return
        if not await self._validate_creator(run_id, config):
            return
        verdict = await self.moderator.moderate_text(config["caption"], stage="caption")
        if not verdict.safe:
            await self._hold(run_id, "caption_moderation_rejected")
            return
        if not await self._record_intent(run_id, operation_key, expected_hash, config, verdict):
            return
        try:
            if sha256(path.read_bytes()).hexdigest() != expected_hash:
                await self._hold(run_id, "final_hash_mismatch")
                return
        except OSError:
            await self._hold(run_id, "final_artifact_missing")
            return
        if self.transfer is None:
            await self._hold(run_id, "publication_transfer_unavailable")
            return
        try:
            video_url = await self.transfer.transfer(
                path=path, sha256=expected_hash, operation_key=operation_key
            )
        # The transfer can complete after its response is lost, so every failure holds.
        except Exception:  # noqa: BLE001
            await self._hold(run_id, "publication_transfer_unknown")
            return
        try:
            result = await self.provider.submit(
                video_url=video_url,
                account_id=config["account_id"],
                caption=config["caption"],
                privacy=config["privacy"],
                ai_disclosure=config["ai_disclosure"],
                operation_key=operation_key,
            )
        except PublishRejected:
            await self._failed(run_id, "publication_rejected")
            return
        except PublicationUnknown as exc:
            await self._hold(run_id, "publication_unknown", getattr(exc, "post_id", None))
            return
        except Exception:  # noqa: BLE001 - an adapter failure leaves effect status unknown
            await self._hold(run_id, "publication_unknown")
            return
        await self._apply_result(run_id, result)

    async def _validate_creator(self, run_id: UUID, config: dict[str, Any]) -> bool:
        """Require a current read-only creator receipt before transferring bytes."""
        run = await self.store.get_run(run_id)
        metadata = run.final_metadata if run is not None else None
        duration = metadata.get("duration_seconds") if isinstance(metadata, dict) else None
        try:
            capabilities = await self.provider.creator_capabilities(config["account_id"])
            capabilities.validate_publication(
                account_id=config["account_id"],
                privacy=config["privacy"],
                duration_seconds=duration,
                interactions={
                    "allow_comment": False,
                    "allow_duet": False,
                    "allow_stitch": False,
                },
            )
        except Exception:  # noqa: BLE001 - never transfer without a current receipt
            await self._hold(run_id, "creator_capabilities_unavailable")
            return False
        return True

    async def reconcile(self, run_id: UUID) -> None:
        """Poll only a persisted provider receipt.  Unknowns remain held."""
        async with self.store.factory() as session:
            run = await session.get(StudioRun, run_id)
            if run is None or run.status != "publishing":
                return
            if run.mode == "fixture":
                await self.publish(run_id)
                return
            post_id = run.publish_request_id
            if not post_id or run.publish_state not in {"accepted", "publishing"}:
                return
        try:
            result = await self.provider.poll(post_id)
        except Exception:  # noqa: BLE001 - polling transport failures are unknown, never success
            await self._hold(run_id, "publication_status_unknown", post_id)
            return
        await self._apply_result(run_id, result)

    async def _prepare(
        self, run_id: UUID
    ) -> tuple[UUID, str, Path, str, dict[str, Any], str] | None:
        async with self.store.factory() as session:
            run = await session.get(StudioRun, run_id)
            if run is None or run.status != "publishing" or run.cancelled:
                return None
            if run.publish_state in {
                "submitting",
                "unknown",
                "accepted",
                "publishing",
                "published",
            }:
                return None
            config = self._publication_config(run.publish_config, fixture=run.mode == "fixture")
            expected_hash = run.final_hash
            if (
                not config
                or not expected_hash
                or self.store.publication_hash(run) != run.approved_publish_hash
            ):
                return None
            decision = await session.scalar(
                select(StudioDecision)
                .where(
                    StudioDecision.run_id == run_id,
                    StudioDecision.visitor_id == run.visitor_id,
                    StudioDecision.gate == "publish",
                    StudioDecision.revision == run.revision,
                    StudioDecision.subject_hash == run.approved_publish_hash,
                    StudioDecision.decision == "approve",
                    StudioDecision.expires_at > _utc_now(),
                )
                .limit(1)
            )
            if decision is None or not self._receipt_matches(
                decision, config, run.approved_publish_hash
            ):
                return None
            path = self.store.config.media_root / str(run_id) / "final.mp4"
        try:
            actual_hash = sha256(path.read_bytes()).hexdigest()
        except OSError:
            return None
        if actual_hash != expected_hash:
            return None
        async with self.store.factory() as session:
            current = await session.get(StudioRun, run_id)
            if current is None or not self._verified_final(current, expected_hash):
                return None
            assets = (
                await session.scalars(
                    select(StudioAsset).where(
                        StudioAsset.run_id == run_id,
                        StudioAsset.revision == current.revision,
                    )
                )
            ).all()
            if not self._safe_current_assets(assets):
                return None
        return (
            run_id,
            f"monthly:{run_id}:{run.revision}:publish",
            path,
            expected_hash,
            config,
            run.mode,
        )

    async def _record_intent(
        self,
        run_id: UUID,
        operation_key: str,
        expected_hash: str,
        config: dict[str, Any],
        verdict: ModerationVerdict,
    ) -> bool:
        async with self.store.transaction() as session:
            run = await session.get(StudioRun, run_id)
            if run is None or run.status != "publishing" or run.publish_state is not None:
                return False
            if (
                self.store.publication_hash(run) != run.approved_publish_hash
                or run.final_hash != expected_hash
            ):
                return False
            if (
                not self._verified_final(run, expected_hash)
                or self._publication_config(run.publish_config) != config
            ):
                return False
            assets = (
                await session.scalars(
                    select(StudioAsset).where(
                        StudioAsset.run_id == run_id,
                        StudioAsset.revision == run.revision,
                    )
                )
            ).all()
            if not self._safe_current_assets(assets):
                return False
            metadata = dict(run.final_metadata or {})
            metadata["caption_moderation"] = {
                "safe": True,
                "reason": verdict.reason[:120],
                "model": verdict.model[:120],
                "coverage": list(verdict.coverage)[:24],
            }
            run.final_metadata = metadata
            run.publish_state = "submitting"
            run.error_code = None
            await self.store.event(
                session,
                run,
                "publishing",
                "publication_intent",
                {"revision": run.revision},
            )
            return True

    async def _apply_result(self, run_id: UUID, result: PublicationResult) -> None:
        if result.state == "failed":
            await self._failed(run_id, "publication_failed")
        elif result.state == "unknown":
            await self._hold(run_id, "publication_status_unknown", result.post_id)
        elif result.state in {"accepted", "publishing"}:
            if result.post_id:
                await self._accepted(run_id, result.post_id, result.state)
            else:
                await self._hold(run_id, "publication_receipt_missing")
        elif result.state == "published":
            if self._valid_tiktok_receipt(result.platform_post_id, result.platform_url):
                await self._published(
                    run_id, result.post_id, result.platform_post_id, result.platform_url
                )
            elif result.post_id:
                await self._accepted(run_id, result.post_id, "publishing")
            else:
                await self._hold(run_id, "publication_receipt_missing")

    async def _complete_fixture(self, run_id: UUID) -> None:
        async with self.store.transaction() as session:
            run = await session.get(StudioRun, run_id)
            if run is None or run.status != "publishing" or run.mode != "fixture":
                return
            run.status, run.publish_state = "simulated_complete", "simulated"
            run.publish_request_id = run.tiktok_post_id = run.tiktok_url = None
            run.error_code = None
            await self.store.close_fixture_reservation(session, run_id)
            await self.store.event(session, run, "simulated_complete", "publication_simulated", {})

    async def _accepted(self, run_id: UUID, post_id: str, state: str = "accepted") -> None:
        async with self.store.transaction() as session:
            run = await session.get(StudioRun, run_id)
            if run is None or run.status != "publishing":
                return
            run.publish_request_id, run.publish_state, run.error_code = (
                post_id[:160],
                state,
                None,
            )
            await self.store.event(session, run, "publishing", "publication_accepted", {})

    async def _published(
        self, run_id: UUID, provider_id: str | None, tiktok_id: str, url: str
    ) -> None:
        async with self.store.transaction() as session:
            run = await session.get(StudioRun, run_id)
            if run is None or run.status != "publishing":
                return
            run.status, run.publish_state = "published", "published"
            run.publish_request_id = (provider_id or run.publish_request_id or "")[:160] or None
            run.tiktok_post_id, run.tiktok_url, run.error_code = tiktok_id, url, None
            await self.store.event(session, run, "published", "publication_resolved", {})

    async def _failed(self, run_id: UUID, code: str) -> None:
        async with self.store.transaction() as session:
            run = await session.get(StudioRun, run_id)
            if run is None or run.status != "publishing":
                return
            run.status, run.publish_state, run.error_code = "failed", "failed", code
            await self.store.event(session, run, "failed", "publication_failed", {"code": code})

    async def _hold(self, run_id: UUID, code: str, post_id: str | None = None) -> None:
        async with self.store.transaction() as session:
            run = await session.get(StudioRun, run_id)
            if run is None or run.status != "publishing":
                return
            if post_id:
                run.publish_request_id = post_id[:160]
            run.status, run.publish_state, run.error_code = (
                "submission_unknown",
                "unknown",
                code,
            )
            await self.store.event(
                session, run, "submission_unknown", "publication_held", {"code": code}
            )

    @staticmethod
    def _publication_config(value: object, *, fixture: bool = False) -> dict[str, Any] | None:
        if not isinstance(value, dict):
            return None
        keys = ("account_id", "caption", "privacy", "ai_disclosure")
        if set(keys) - value.keys() or not all(
            isinstance(value[key], str)
            and (value[key].strip() or (fixture and key == "account_id"))
            for key in keys[:3]
        ):
            return None
        if not isinstance(value["ai_disclosure"], bool):
            return None
        return {key: value[key] for key in keys}

    @staticmethod
    def _receipt_matches(
        decision: StudioDecision, config: dict[str, Any], subject_hash: str
    ) -> bool:
        return (
            decision.gate == "publish"
            and decision.decision == "approve"
            and decision.subject_hash == subject_hash
            and isinstance(decision.payload, dict)
            and all(decision.payload.get(key) == value for key, value in config.items())
        )

    @staticmethod
    def _verified_final(run: StudioRun, expected_hash: str) -> bool:
        metadata = run.final_metadata
        return bool(
            isinstance(metadata, dict)
            and metadata.get("media_verified") is True
            and metadata.get("sha256") == expected_hash
            and run.final_hash == expected_hash
        )

    @staticmethod
    def _safe_current_assets(assets: list[StudioAsset]) -> bool:
        current: dict[tuple[int, str], StudioAsset] = {}
        for asset in assets:
            key = (int(asset.ordinal), str(asset.kind))
            if key not in current or asset.attempt > current[key].attempt:
                current[key] = asset
        return len(current) == 24 and all(
            1 <= ordinal <= 12
            and kind in {"video", "voice"}
            and asset.status == "ready"
            and isinstance(asset.safety, dict)
            and asset.safety.get("safe") is True
            for (ordinal, kind), asset in current.items()
        )

    @staticmethod
    def _valid_tiktok_receipt(post_id: object, url: object) -> bool:
        if not isinstance(post_id, str) or not post_id.isdigit() or not 10 <= len(post_id) <= 40:
            return False
        if not isinstance(url, str):
            return False
        parsed = urlparse(url)
        return (
            parsed.scheme == "https"
            and (parsed.hostname == "tiktok.com" or (parsed.hostname or "").endswith(".tiktok.com"))
            and f"/video/{post_id}" in parsed.path
        )
