from __future__ import annotations

from hashlib import sha256

import pytest

from myloware.studio.zernio_transfer import ZernioMediaTransfer


class Response:
    def __init__(self, status_code: int, payload: object = None) -> None:
        self.status_code, self.payload = status_code, payload

    def json(self) -> object:
        return self.payload


class Client:
    def __init__(self, payload: object) -> None:
        self.payload, self.posts, self.puts = payload, [], []

    async def post(self, url: str, **kwargs):  # type: ignore[no-untyped-def]
        self.posts.append((url, kwargs))
        return Response(200, self.payload)

    async def put(self, url: str, **kwargs):  # type: ignore[no-untyped-def]
        self.puts.append((url, kwargs))
        return Response(200)


def payload(**overrides):  # type: ignore[no-untyped-def]
    return {
        "uploadUrl": "https://bucket.r2.cloudflarestorage.com/temp/video.mp4?X-Amz-Signature=abc",
        "publicUrl": "https://media.zernio.com/temp/video.mp4",
        **overrides,
    }


@pytest.mark.asyncio
async def test_presign_transfer_uploads_exact_bytes_without_bearer_to_storage(
    tmp_path,
) -> None:
    path = tmp_path / "final.mp4"
    path.write_bytes(b"reviewed MP4 bytes")
    client = Client(payload())
    public_url = await ZernioMediaTransfer(client, client, api_key="secret-key").transfer(
        path=path,
        sha256=sha256(path.read_bytes()).hexdigest(),
        operation_key="publish-1",
    )
    assert public_url == "https://media.zernio.com/temp/video.mp4"
    presign_url, presign = client.posts[0]
    assert presign_url == "https://zernio.com/api/v1/media/presign"
    assert presign["headers"]["Authorization"] == "Bearer secret-key"
    assert presign["json"] == {
        "filename": f"aismr-{sha256(path.read_bytes()).hexdigest()[:16]}.mp4",
        "contentType": "video/mp4",
        "size": len(path.read_bytes()),
    }
    _, upload = client.puts[0]
    assert upload == {
        "content": b"reviewed MP4 bytes",
        "headers": {"Content-Type": "video/mp4"},
        "follow_redirects": False,
    }


@pytest.mark.asyncio
async def test_untrusted_presigned_origin_never_receives_bytes_or_credentials(
    tmp_path,
) -> None:
    path = tmp_path / "final.mp4"
    path.write_bytes(b"reviewed MP4 bytes")
    client = Client(payload(uploadUrl="https://evil.example/upload?token=x"))
    with pytest.raises(RuntimeError, match="untrusted URL"):
        await ZernioMediaTransfer(client, client, api_key="secret-key").transfer(
            path=path,
            sha256=sha256(path.read_bytes()).hexdigest(),
            operation_key="publish-1",
        )
    assert client.puts == []


@pytest.mark.asyncio
async def test_mismatched_review_hash_never_requests_presign(tmp_path) -> None:
    path = tmp_path / "final.mp4"
    path.write_bytes(b"changed bytes")
    client = Client(payload())
    with pytest.raises(ValueError, match="reviewed SHA-256"):
        await ZernioMediaTransfer(client, client, api_key="secret-key").transfer(
            path=path, sha256="0" * 64, operation_key="publish-1"
        )
    assert client.posts == client.puts == []
