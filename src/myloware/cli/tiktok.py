"""Commands for the explicit owner-operated native TikTok sandbox test."""

from __future__ import annotations

import asyncio
import logging
import stat
from pathlib import Path

import click
import httpx
import uvicorn
from dotenv import dotenv_values
from openai import AsyncOpenAI

from myloware.providers.tiktok_native import NativeTikTokClient
from myloware.studio.moderation import OpenAIModerator
from myloware.studio.tiktok_owner import (
    ApprovedVideo,
    OwnerTikTokSession,
    create_apps,
    write_private_json,
)


def load_credentials(path: Path) -> dict[str, str]:
    if not stat.S_ISFIFO(path.stat().st_mode):
        raise ValueError("TikTok credentials must come from the native 1Password FIFO")
    keys = ("AISMR_TIKTOK_CLIENT_KEY", "AISMR_TIKTOK_CLIENT_SECRET", "OPENAI_API_KEY")
    loaded = dotenv_values(path)
    if any(not loaded.get(key) for key in keys):
        raise ValueError("The AISMR environment is missing native TikTok or moderation credentials")
    result = {key: str(loaded[key]) for key in keys}
    loaded.clear()
    return result


async def run_server(
    credentials: Path,
    artifact: Path,
    approval: Path,
    receipt: Path,
    callback_origin: str,
    port: int,
    callback_port: int,
    ready_file: Path,
) -> None:
    values = await asyncio.to_thread(load_credentials, credentials)
    video = await asyncio.to_thread(ApprovedVideo.from_receipt, artifact, approval)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    async with (
        httpx.AsyncClient(timeout=60, follow_redirects=False) as http,
        AsyncOpenAI(api_key=values.pop("OPENAI_API_KEY"), max_retries=0) as moderation,
    ):
        native = NativeTikTokClient(
            http,
            client_key=values.pop("AISMR_TIKTOK_CLIENT_KEY"),
            client_secret=values.pop("AISMR_TIKTOK_CLIENT_SECRET"),
        )
        session = OwnerTikTokSession(
            native,
            OpenAIModerator(moderation),
            video,
            receipt,
            callback_origin,
            owner_port=port,
        )
        owner, callback = create_apps(session)
        # This owner-only local file has a one-use bootstrap link, never provider secrets.
        write_private_json(
            ready_file,
            {
                "owner_url": session.owner_origin + "/connect/" + session.bootstrap,
                "redirect_uri": session.redirect_uri,
                "callback_port": callback_port,
                "owner_port": port,
                "artifact_sha256": video.sha256,
            },
        )
        click.echo(f"AISMR native TikTok owner UI: {session.owner_origin}")
        click.echo(f"Register this sandbox redirect URI: {session.redirect_uri}")
        owner_server = uvicorn.Server(
            uvicorn.Config(
                owner,
                host="127.0.0.1",
                port=port,
                access_log=False,
                log_level="warning",
                proxy_headers=False,
            )
        )
        callback_server = uvicorn.Server(
            uvicorn.Config(
                callback,
                host="127.0.0.1",
                port=callback_port,
                access_log=False,
                log_level="warning",
                proxy_headers=False,
            )
        )
        try:
            await asyncio.gather(owner_server.serve(), callback_server.serve())
        finally:
            for task in tuple(session.tasks):
                task.cancel()
            if session.tasks:
                await asyncio.gather(*session.tasks, return_exceptions=True)
            session.close()
            ready_file.unlink(missing_ok=True)


@click.group("tiktok")
def tiktok_group() -> None:
    """Connect the native TikTok sandbox for a reviewed local video."""


@tiktok_group.command("serve")
@click.option("--credentials", type=click.Path(path_type=Path, exists=True), required=True)
@click.option("--artifact", type=click.Path(path_type=Path, exists=True), required=True)
@click.option("--approval", type=click.Path(path_type=Path, exists=True), required=True)
@click.option("--receipt", type=click.Path(path_type=Path), required=True)
@click.option(
    "--callback-origin", required=True, help="Exact HTTPS origin registered for this session."
)
@click.option("--port", default=8315, type=click.IntRange(1024, 65535))
@click.option("--callback-port", default=8316, type=click.IntRange(1024, 65535))
@click.option("--ready-file", type=click.Path(path_type=Path), required=True)
def serve(**options: object) -> None:
    """Start local review and callback-only servers; this does not post a video."""
    if options["port"] == options["callback_port"]:
        raise click.ClickException("Owner and callback ports must be different")
    try:
        asyncio.run(run_server(**options))  # type: ignore[arg-type]
    except (OSError, ValueError) as exc:
        raise click.ClickException(f"Native TikTok startup failed ({type(exc).__name__})") from None


def register(cli: click.Group) -> None:
    cli.add_command(tiktok_group)
