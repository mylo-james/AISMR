"""One-turn, read-only Codex CLI transport for monthly ideation.

This staging artifact deliberately uses only the standard library.  It exposes
the small async ``chat.completions.create`` shape consumed by MonthlyIdeator.
It does not start a process until ``create`` is called.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import signal
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any


class CodexIdeationTransportError(RuntimeError):
    """The isolated Codex turn did not produce a safe, valid completion."""


@dataclass(frozen=True)
class CodexRunReceipt:
    cli: str
    model: str
    timeout_seconds: int
    input_bytes: int
    output_bytes: int
    stderr_bytes: int
    event_types: tuple[str, ...]
    usage: Mapping[str, Any] | None


class CodexIdeationClient:
    """Duck-types the async OpenAI chat-completions client used by MonthlyIdeator."""

    _DISABLED_FEATURES = (
        "shell_tool",
        "apps",
        "plugins",
        "hooks",
        "memories",
        "browser_use",
        "computer_use",
        "image_generation",
        "skill_search",
        "skill_mcp_dependency_install",
    )
    _FORBIDDEN_EVENT = re.compile(
        r"(?:tool|command|shell|exec|mcp|app|browser|computer|image|patch|file)", re.IGNORECASE
    )
    _SENSITIVE_DIAGNOSTIC = re.compile(
        r"(?ix)"
        r"(authorization\s*[:=]\s*(?:bearer\s+)?|"
        r"(?:api[_-]?key|token|secret|password)\s*[:=]\s*)[^\s,;]+|"
        r"\b(?:sk|rk|sess)-[a-z0-9_-]{8,}\b|"
        r"\beyJ[a-z0-9_-]+\.[a-z0-9_-]+\.[a-z0-9_-]+\b"
    )

    def __init__(
        self,
        *,
        codex_bin: str = "/opt/homebrew/bin/codex",
        model: str = "gpt-5.6-luna",
        timeout_seconds: int = 90,
        max_jsonl_bytes: int = 262_144,
        max_response_bytes: int = 65_536,
        max_input_bytes: int = 32_768,
        local_root: Path | str = "/private/tmp/aismr-codex-transport",
        process_factory: Any = asyncio.create_subprocess_exec,
    ) -> None:
        if not 1 <= timeout_seconds <= 90:
            raise ValueError("timeout_seconds must be between 1 and 90")
        if min(max_jsonl_bytes, max_response_bytes, max_input_bytes) < 1:
            raise ValueError("byte limits must be positive")
        self._codex_bin = str(codex_bin)
        self._model = model
        self._timeout_seconds = timeout_seconds
        self._max_jsonl_bytes = max_jsonl_bytes
        self._max_response_bytes = max_response_bytes
        self._max_input_bytes = max_input_bytes
        self._local_root = Path(local_root)
        self._process_factory = process_factory
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))
        self.last_receipt: CodexRunReceipt | None = None

    async def create(self, **kwargs: Any) -> Any:
        """Run exactly one schema-constrained turn and return an OpenAI-like response."""
        if kwargs.get("stream"):
            raise CodexIdeationTransportError("streaming is not supported")
        if kwargs.get("tools", []) not in ([], None) or kwargs.get("parallel_tool_calls"):
            raise CodexIdeationTransportError("tools must be disabled")
        messages = kwargs.get("messages")
        if not isinstance(messages, Sequence) or not messages:
            raise CodexIdeationTransportError("a non-empty messages sequence is required")
        schema = self._extract_schema(kwargs.get("response_format"))
        prompt = self._prompt(messages)
        if len(prompt.encode("utf-8")) > self._max_input_bytes:
            raise CodexIdeationTransportError("Codex ideation input exceeded its byte limit")

        self._local_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="run-", dir=self._local_root) as run_dir:
            run = Path(run_dir)
            empty_cwd = run / "cwd"
            empty_cwd.mkdir(mode=0o700)
            schema_path = run / "response-schema.json"
            schema_path.write_text(json.dumps(schema, separators=(",", ":")), encoding="utf-8")
            args = self._argv(empty_cwd, schema_path)
            output, stderr_bytes = await self._run(args, prompt)
        content, receipt = self._parse(output, len(prompt.encode("utf-8")), stderr_bytes)
        self.last_receipt = receipt
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])

    def _argv(self, cwd: Path, schema_path: Path) -> list[str]:
        args = [
            self._codex_bin,
            "exec",
            "--ignore-user-config",
            "--ephemeral",
            "--skip-git-repo-check",
            "--strict-config",
            "-C",
            str(cwd),
            "-s",
            "read-only",
            "-m",
            self._model,
            "-c",
            'model_reasoning_effort="low"',
            "-c",
            'approval_policy="never"',
            "-c",
            'web_search="disabled"',
            "-c",
            "mcp_servers={}",
            "-c",
            "project_doc_max_bytes=0",
        ]
        for feature in self._DISABLED_FEATURES:
            args.extend(("--disable", feature))
        return args + ["--output-schema", str(schema_path), "--json", "-"]

    @staticmethod
    def _extract_schema(response_format: Any) -> Mapping[str, Any]:
        if not isinstance(response_format, Mapping):
            raise CodexIdeationTransportError("a JSON Schema response format is required")
        schema = response_format.get("json_schema", {}).get("schema")
        if not isinstance(schema, Mapping):
            raise CodexIdeationTransportError("response schema is missing")
        return schema

    @staticmethod
    def _prompt(messages: Sequence[Any]) -> str:
        blocks: list[str] = []
        for message in messages:
            if not isinstance(message, Mapping):
                raise CodexIdeationTransportError("messages must be mappings")
            role, content = message.get("role"), message.get("content")
            if role not in {"system", "user"} or not isinstance(content, str):
                raise CodexIdeationTransportError(
                    "only string system and user messages are supported"
                )
            blocks.append(f"{str(role).upper()}:\n{content}")
        return "\n\n".join(blocks)

    def _child_env(self) -> dict[str, str]:
        # HOME retains the native login location.  No provider-key variables and no
        # CODEX_HOME override are passed to the child.
        return {
            name: os.environ[name] for name in ("HOME", "PATH", "TMPDIR") if os.environ.get(name)
        }

    async def _run(self, args: list[str], prompt: str) -> tuple[bytes, int]:
        try:
            process = await self._process_factory(
                *args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=self._child_env(),
                start_new_session=True,
            )
        except OSError as exc:
            raise CodexIdeationTransportError("Codex executable could not start") from exc
        if process.stdin is None or process.stdout is None or process.stderr is None:
            await self._terminate_group(process)
            raise CodexIdeationTransportError("Codex subprocess pipes are unavailable")
        stdin, process_stdout, process_stderr = process.stdin, process.stdout, process.stderr
        stdout: list[bytes] = []
        stderr_digest = hashlib.sha256()
        stderr_categories: set[str] = set()
        safe_diagnostic: list[str] = []
        combined = 0
        counter_lock = asyncio.Lock()

        async def read_stream(reader: asyncio.StreamReader, *, retain: bool) -> int:
            nonlocal combined
            count = 0
            while chunk := await reader.read(8192):
                count += len(chunk)
                async with counter_lock:
                    combined += len(chunk)
                    if combined > self._max_jsonl_bytes:
                        raise CodexIdeationTransportError(
                            "Codex ideation output exceeded its byte limit"
                        )
                if retain:
                    stdout.append(chunk)
                else:
                    stderr_digest.update(chunk)
                    diagnostic = chunk.lower()
                    if len("".join(safe_diagnostic)) < 1000:
                        text = chunk.decode("utf-8", "replace")
                        text = self._SENSITIVE_DIAGNOSTIC.sub("[REDACTED]", text)
                        safe_diagnostic.append(text[: 1000 - len("".join(safe_diagnostic))])
                    for needle, category in (
                        (b"unknown configuration", "unknown-config"),
                        (b"unknown key", "unknown-config"),
                        (b"unknown feature", "unknown-feature"),
                        (b"invalid value", "invalid-value"),
                        (b"authentication", "authentication"),
                        (b"not logged", "authentication"),
                        (b"network", "network"),
                    ):
                        if needle in diagnostic:
                            stderr_categories.add(category)
            return count

        async def write_stdin() -> None:
            stdin.write(prompt.encode("utf-8"))
            await stdin.drain()
            stdin.close()
            await stdin.wait_closed()

        stdout_task = asyncio.create_task(read_stream(process_stdout, retain=True))
        stderr_task = asyncio.create_task(read_stream(process_stderr, retain=False))
        writer_task = asyncio.create_task(write_stdin())
        wait_task = asyncio.create_task(process.wait())
        tasks = (stdout_task, stderr_task, writer_task, wait_task)
        try:
            async with asyncio.timeout(self._timeout_seconds):
                await asyncio.gather(*tasks)
        except TimeoutError as exc:
            await self._terminate_group(process)
            raise CodexIdeationTransportError("Codex ideation deadline exceeded") from exc
        except BaseException:
            await self._terminate_group(process)
            raise
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if process.returncode is None:
                await self._terminate_group(process)
        if process.returncode:
            raise CodexIdeationTransportError(
                "Codex ideation process failed "
                f"(exit={process.returncode}, categories={sorted(stderr_categories)}, "
                f"stderr_sha256={stderr_digest.hexdigest()}): "
                f"{''.join(safe_diagnostic).strip() or 'no diagnostic text'}"
            )
        # Diagnostics are deliberately discarded. Their byte count is retained only
        # for a non-sensitive receipt, because successful CLI runs can warn on stderr.
        return b"".join(stdout), stderr_task.result()

    @staticmethod
    async def _terminate_group(process: Any) -> None:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        try:
            await asyncio.wait_for(process.wait(), timeout=3)
        except TimeoutError:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await process.wait()

    def _parse(
        self, output: bytes, input_bytes: int, stderr_bytes: int
    ) -> tuple[str, CodexRunReceipt]:
        event_types: list[str] = []
        final_messages: list[str] = []
        completed = False
        usage: Mapping[str, Any] | None = None
        for line in output.splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError as exc:
                raise CodexIdeationTransportError("Codex ideation emitted malformed JSONL") from exc
            if not isinstance(event, Mapping) or not isinstance(event.get("type"), str):
                raise CodexIdeationTransportError("Codex ideation emitted an invalid event")
            kind = event["type"]
            event_types.append(kind)
            if kind not in {
                "thread.started",
                "turn.started",
                "item.started",
                "item.completed",
                "turn.completed",
            }:
                raise CodexIdeationTransportError("Codex ideation emitted an unsupported event")
            if completed:
                raise CodexIdeationTransportError(
                    "Codex ideation emitted an event after completion"
                )
            item = event.get("item")
            item_kind = item.get("type") if isinstance(item, Mapping) else ""
            if self._FORBIDDEN_EVENT.search(kind) or self._FORBIDDEN_EVENT.search(str(item_kind)):
                raise CodexIdeationTransportError("Codex ideation emitted a forbidden effect event")
            if kind == "turn.completed":
                if completed:
                    raise CodexIdeationTransportError("Codex ideation emitted duplicate completion")
                completed = True
                candidate = event.get("usage")
                usage = candidate if isinstance(candidate, Mapping) else None
            if kind == "item.completed" and item_kind == "agent_message":
                text = item.get("text") if isinstance(item, Mapping) else None
                if not isinstance(text, str):
                    raise CodexIdeationTransportError("Codex agent message was invalid")
                final_messages.append(text)
        if not completed or len(final_messages) != 1:
            raise CodexIdeationTransportError(
                "Codex ideation did not emit one completed agent message"
            )
        content = final_messages[0]
        if len(content.encode("utf-8")) > self._max_response_bytes:
            raise CodexIdeationTransportError("Codex ideation response exceeded its byte limit")
        try:
            json.loads(content)
        except json.JSONDecodeError as exc:
            raise CodexIdeationTransportError("Codex ideation response was not JSON") from exc
        return content, CodexRunReceipt(
            cli=self._codex_bin,
            model=self._model,
            timeout_seconds=self._timeout_seconds,
            input_bytes=input_bytes,
            output_bytes=len(output),
            stderr_bytes=stderr_bytes,
            event_types=tuple(event_types),
            usage=usage,
        )
