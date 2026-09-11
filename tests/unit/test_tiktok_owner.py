import asyncio
import hashlib
import json
from contextlib import asynccontextmanager
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from click.testing import CliRunner

from myloware.cli.tiktok import load_credentials, tiktok_group
from myloware.providers.tiktok_native import (
    CreatorInfo,
    PostStatus,
    TikTokApiError,
    TikTokInitializedUploadError,
    TikTokToken,
    UploadInfo,
)
from myloware.studio.moderation import ModerationVerdict
from myloware.studio.tiktok_owner import ApprovedVideo, OwnerTikTokSession, create_apps


class Moderator:
    safe = True

    async def moderate_text(self, text, *, stage):
        return ModerationVerdict(self.safe, "test", "test-moderation", (stage,))


class Native:
    def __init__(self, receipt):
        self.receipt = receipt
        self.username = "aismr698"
        self.init_calls = 0
        self.upload_calls = 0
        self.fail_init = False
        self.init_error = None
        self.status_calls = 0
        self.mutate_source = None

    def authorization_url(self, redirect_uri, state):
        return "https://www.tiktok.com/v2/auth/authorize/?state=" + state

    async def exchange_code(self, code, redirect_uri):
        return TikTokToken(
            "access-secret",
            "refresh-secret",
            "owner-id",
            86400,
            ("user.info.basic", "video.publish"),
        )

    async def creator_info(self, access_token):
        return CreatorInfo(self.username, "AISMR", ("SELF_ONLY",), True, True, True, 600)

    async def initialize_video(self, access_token, size, caption, **settings):
        assert json.loads(self.receipt.read_text())["state"] == "initializing"
        self.init_calls += 1
        if self.mutate_source is not None:
            self.mutate_source.write_bytes(b"wrong-data")
        if self.init_error:
            raise self.init_error
        if self.fail_init:
            raise TikTokApiError("unknown_error")
        return UploadInfo(
            "native-publish-id", "https://open-upload.tiktokapis.com/video", size, 1, size
        )

    async def upload_stream(self, info, source):
        assert json.loads(self.receipt.read_text())["publish_id"] == info.publish_id
        assert source.read() == b"test-media"
        self.upload_calls += 1

    async def status(self, access_token, publish_id):
        self.status_calls += 1
        return PostStatus("PUBLISH_COMPLETE", None, (), 10)


@pytest.fixture
def session(tmp_path):
    video_path = tmp_path / "approved.mp4"
    video_path.write_bytes(b"test-media")
    video = ApprovedVideo(
        video_path, hashlib.sha256(b"test-media").hexdigest(), 10, 80.8, "Dreamlike teacups"
    )
    native = Native(tmp_path / "native.json")
    result = OwnerTikTokSession(native, Moderator(), video, native.receipt, "https://test")
    yield result
    result.close()


@asynccontextmanager
async def open_owner(session):
    owner, callback = create_apps(session)
    async with (
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=owner), base_url=session.owner_origin
        ) as client,
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=callback), base_url="https://test"
        ) as public,
    ):
        await client.get("/connect/" + session.bootstrap)
        yield client, public


async def connect(session, owner, public):
    response = await owner.post(
        "/authorize", data={"csrf": session.csrf}, headers={"Origin": session.owner_origin}
    )
    start = await public.get(response.headers["location"])
    state = parse_qs(urlsplit(start.headers["location"]).query)["state"][0]
    return await public.get("/auth/tiktok/callback", params={"state": state, "code": "code"})


def consent(session, **changes):
    return {
        "csrf": session.csrf,
        "privacy": "SELF_ONLY",
        "private_account": "yes",
        "consent": "yes",
        "caption": "Dreamlike teacups",
        **changes,
    }


async def wait_upload(session):
    if session.tasks:
        await asyncio.gather(*tuple(session.tasks))


@pytest.mark.asyncio
async def test_oauth_is_browser_bound_one_time_and_public_app_excludes_owner_routes(session):
    async with open_owner(session) as (owner, public):
        response = await owner.post(
            "/authorize", data={"csrf": session.csrf}, headers={"Origin": session.owner_origin}
        )
        start_url = response.headers["location"]
        started = await public.get(start_url)
        state = parse_qs(urlsplit(started.headers["location"]).query)["state"][0]
        assert (await public.get(start_url)).status_code == 403
        cookie = public.cookies.get("aismr_oauth")
        public.cookies.clear()
        assert (
            await public.get("/auth/tiktok/callback", params={"state": state, "code": "code"})
        ).status_code == 403
        public.cookies.set("aismr_oauth", cookie)
        succeeded = await public.get(
            "/auth/tiktok/callback", params={"state": state, "code": "code"}
        )
        assert succeeded.status_code == 200
        assert succeeded.headers["Referrer-Policy"] == "no-referrer"
        assert session.owner_key not in succeeded.text
        assert "access-secret" not in succeeded.text
        assert (
            await public.get("/auth/tiktok/callback", params={"state": state, "code": "code"})
        ).status_code == 403
        for path in ("/media", "/state", "/", "/docs", "/openapi.json"):
            assert (await public.get(path)).status_code == 404


@pytest.mark.asyncio
async def test_owner_origin_cookie_csrf_and_bootstrap_are_enforced(session):
    owner_app, _ = create_apps(session)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=owner_app), base_url=session.owner_origin
    ) as owner:
        assert (await owner.get("/state")).status_code == 403
        ticket = session.bootstrap
        assert (await owner.get("/connect/" + ticket)).status_code == 303
        assert (await owner.get("/connect/" + ticket)).status_code == 403
        assert (await owner.get("/state", headers={"Host": "attacker.example"})).status_code == 403
        assert (await owner.post("/authorize", data={"csrf": session.csrf})).status_code == 403
        assert (await owner.get("/")).headers["Referrer-Policy"] == "same-origin"
        assert (
            await owner.post("/authorize", data={"csrf": session.csrf}, headers={"Origin": "null"})
        ).status_code == 403
        assert (
            await owner.post(
                "/authorize", data={"csrf": "wrong"}, headers={"Origin": session.owner_origin}
            )
        ).status_code == 403


@pytest.mark.asyncio
async def test_wrong_account_does_not_gain_a_token(session):
    session.client.username = "someone_else"
    async with open_owner(session) as (owner, public):
        assert (await connect(session, owner, public)).status_code == 400
        assert session.token is None
        assert session.client.init_calls == 0


@pytest.mark.asyncio
async def test_concurrent_submit_has_one_init_and_durable_id_before_transfer(session):
    async with open_owner(session) as (owner, public):
        assert (await connect(session, owner, public)).status_code == 200
        page = (await owner.get("/")).text
        assert '<option value="" selected disabled>' in page
        assert 'value="yes" disabled' in page
        responses = await asyncio.gather(
            *[
                owner.post(
                    "/publish", data=consent(session), headers={"Origin": session.owner_origin}
                )
                for _ in range(2)
            ]
        )
        await wait_upload(session)
        assert sorted(r.status_code for r in responses) == [303, 409]
        assert session.client.init_calls == session.client.upload_calls == 1
        stored = json.loads(session.receipt_path.read_text())
        assert stored["state"] == "processing"
        assert stored["automatic_retries"] == 0
        assert stored["privacy"] == "SELF_ONLY"
        assert "access-secret" not in session.receipt_path.read_text()
        assert session.receipt_path.stat().st_mode & 0o777 == 0o600
        assert (
            await owner.post(
                "/status", data={"csrf": session.csrf}, headers={"Origin": session.owner_origin}
            )
        ).status_code == 303
        assert session.receipt["state"] == "published"
        assert session.receipt["public_post_ids"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changes",
    [
        {"privacy": ""},
        {"privacy": "PUBLIC_TO_EVERYONE"},
        {"consent": ""},
        {"private_account": ""},
        {"promotes_brand": "yes"},
        {"allow_comment": "yes"},
    ],
)
async def test_missing_or_incompatible_consent_never_initializes(session, changes):
    async with open_owner(session) as (owner, public):
        await connect(session, owner, public)
        result = await owner.post(
            "/publish", data=consent(session, **changes), headers={"Origin": session.owner_origin}
        )
        assert result.status_code == 400
        assert not session.receipt
        assert session.client.init_calls == 0


@pytest.mark.asyncio
async def test_moderation_denial_and_changed_artifact_prevent_init(session):
    async with open_owner(session) as (owner, public):
        await connect(session, owner, public)
        session.moderator.safe = False
        result = await owner.post(
            "/publish", data=consent(session), headers={"Origin": session.owner_origin}
        )
        assert result.status_code == 400
        assert session.client.init_calls == 0
        session.moderator.safe = True
        session.video.path.write_bytes(b"wrong-data")
        result = await owner.post(
            "/publish", data=consent(session), headers={"Origin": session.owner_origin}
        )
        assert result.status_code == 409
        assert session.client.init_calls == 0


@pytest.mark.asyncio
async def test_unknown_init_is_held_after_restart_without_reposting(session):
    session.client.fail_init = True
    async with open_owner(session) as (owner, public):
        await connect(session, owner, public)
        await owner.post(
            "/publish", data=consent(session), headers={"Origin": session.owner_origin}
        )
        await wait_upload(session)
    assert session.receipt["state"] == "unknown"
    assert not session.receipt.get("publish_id")
    session.close()
    restored = OwnerTikTokSession(
        session.client, session.moderator, session.video, session.receipt_path, "https://test"
    )
    try:
        async with open_owner(restored) as (owner, public):
            await connect(restored, owner, public)
            result = await owner.post(
                "/publish", data=consent(restored), headers={"Origin": restored.owner_origin}
            )
            assert result.status_code == 409
        assert session.client.init_calls == 1
    finally:
        restored.close()


def test_receipt_lock_and_credential_mount_boundary(session, tmp_path):
    with pytest.raises(ValueError, match="Another process"):
        OwnerTikTokSession(
            session.client, session.moderator, session.video, session.receipt_path, "https://test"
        )
    ordinary_env = tmp_path / "ordinary.env"
    ordinary_env.write_text("AISMR_TIKTOK_CLIENT_KEY=bad")
    with pytest.raises(ValueError, match="1Password FIFO"):
        load_credentials(ordinary_env)


def test_cli_help_never_reads_credentials():
    result = CliRunner().invoke(tiktok_group, ["serve", "--help"])
    assert result.exit_code == 0
    assert "--callback-origin" in result.output
    assert "--approval" in result.output


def test_approved_video_requires_real_hash_approval_and_moderation(tmp_path):
    media = tmp_path / "video.mp4"
    media.write_bytes(b"sample")
    receipt = tmp_path / "approval.json"
    payload = {
        "sha256": hashlib.sha256(b"sample").hexdigest(),
        "bytes": 6,
        "owner_approval": {"statement": "Approved", "approved_artifact": str(media)},
        "moderation": {"verdicts": [{"safe": True}], "frames": [{"name": "sample"}]},
        "config": {"caption": "Dreamlike teacups"},
        "duration_seconds": 80.8,
    }
    receipt.write_text(json.dumps(payload))
    assert ApprovedVideo.from_receipt(media, receipt).size == 6
    payload["moderation"]["verdicts"][0]["safe"] = False
    receipt.write_text(json.dumps(payload))
    with pytest.raises(ValueError, match="approval and moderation"):
        ApprovedVideo.from_receipt(media, receipt)


@pytest.mark.asyncio
async def test_upload_uses_verified_anonymous_snapshot_after_source_replacement(session):
    session.client.mutate_source = session.video.path
    async with open_owner(session) as (owner, public):
        await connect(session, owner, public)
        result = await owner.post(
            "/publish", data=consent(session), headers={"Origin": session.owner_origin}
        )
        assert result.status_code == 303
        await wait_upload(session)
        assert session.video.path.read_bytes() == b"wrong-data"
        assert session.client.upload_calls == 1
        assert session.receipt["state"] == "processing"


@pytest.mark.asyncio
async def test_explicit_invalid_params_rejection_is_distinct_from_unknown_and_never_retries(
    session,
):
    session.client.init_error = TikTokApiError("invalid_params", 400)
    async with open_owner(session) as (owner, public):
        await connect(session, owner, public)
        await owner.post(
            "/publish", data=consent(session), headers={"Origin": session.owner_origin}
        )
        await wait_upload(session)
        assert session.receipt["state"] == "rejected"
        assert session.receipt["http_status"] == 400
        assert not session.receipt.get("publish_id")
        assert session.client.upload_calls == 0
        response = await owner.post(
            "/publish", data=consent(session), headers={"Origin": session.owner_origin}
        )
        assert response.status_code == 409
        assert session.client.init_calls == 1


@pytest.mark.asyncio
async def test_initialized_response_failure_retains_publish_id_without_upload_or_retry(session):
    session.client.init_error = TikTokInitializedUploadError(
        "created-native-id", "upload_url_rejected", "untrusted.example"
    )
    async with open_owner(session) as (owner, public):
        await connect(session, owner, public)
        await owner.post(
            "/publish", data=consent(session), headers={"Origin": session.owner_origin}
        )
        await wait_upload(session)
        assert session.receipt["state"] == "held"
        assert session.receipt["publish_id"] == "created-native-id"
        assert session.receipt["provider_effect_confirmed"] is True
        assert session.receipt["transfer_complete"] is False
        assert session.client.upload_calls == 0
        result = await owner.post(
            "/publish", data=consent(session), headers={"Origin": session.owner_origin}
        )
        assert result.status_code == 409
        assert session.client.init_calls == 1
        status = await owner.post(
            "/status", data={"csrf": session.csrf}, headers={"Origin": session.owner_origin}
        )
        assert status.status_code == 303
        assert session.client.status_calls == 1
