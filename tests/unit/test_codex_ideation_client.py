from __future__ import annotations

import asyncio
import json
import os
import stat
import sys
from pathlib import Path

import pytest

from myloware.studio.codex_ideation_client import (
    CodexIdeationClient,
    CodexIdeationTransportError,
)

SCHEMA = {"type": "object", "required": ["ideas"]}
MESSAGES = [{"role": "system", "content": "JSON only"}, {"role": "user", "content": "ideas"}]


def _exe(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "fake-codex.py"
    path.write_text("#!" + sys.executable + "\n" + body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


@pytest.mark.asyncio
async def test_argv_stdin_and_jsonl_protocol_are_bounded(tmp_path: Path) -> None:
    receipt = tmp_path / "received.json"
    exe = _exe(
        tmp_path,
        "import json, os, sys\n"
        f"out = {str(receipt)!r}\n"
        "json.dump({'argv': sys.argv[1:], 'stdin': sys.stdin.read(), 'env': dict(os.environ)}, open(out, 'w'))\n"
        "print(json.dumps({'type':'thread.started'}))\n"
        "print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':'{\\\"ideas\\\":[]}'}}))\n"
        "print(json.dumps({'type':'turn.completed','usage':{'input_tokens':2}}))\n",
    )
    client = CodexIdeationClient(codex_bin=str(exe), local_root=tmp_path / "runs")
    response = await client.chat.completions.create(
        model="ignored",
        messages=MESSAGES,
        response_format={"type": "json_schema", "json_schema": {"schema": SCHEMA}},
        tools=[],
        parallel_tool_calls=False,
        stream=False,
    )
    captured = json.loads(receipt.read_text())
    assert json.loads(response.choices[0].message.content) == {"ideas": []}
    assert "--ignore-user-config" in captured["argv"]
    assert "--ephemeral" in captured["argv"] and "read-only" in captured["argv"]
    assert "gpt-5.6-luna" in captured["argv"] and 'web_search="disabled"' in captured["argv"]
    assert "OPENAI_API_KEY" not in captured["env"] and "CODEX_HOME" not in captured["env"]
    assert captured["stdin"] == "SYSTEM:\nJSON only\n\nUSER:\nideas"
    assert client.last_receipt and client.last_receipt.usage == {"input_tokens": 2}


@pytest.mark.asyncio
async def test_timeout_terminates_process_group(tmp_path: Path) -> None:
    exe = _exe(tmp_path, "import time\ntime.sleep(10)\n")
    client = CodexIdeationClient(
        codex_bin=str(exe), timeout_seconds=1, local_root=tmp_path / "runs"
    )
    with pytest.raises(CodexIdeationTransportError, match="deadline"):
        await client.chat.completions.create(
            messages=MESSAGES, response_format={"json_schema": {"schema": SCHEMA}}, tools=[]
        )


@pytest.mark.asyncio
async def test_rejects_tool_events_and_nonfinal_output(tmp_path: Path) -> None:
    exe = _exe(
        tmp_path,
        "import json\nprint(json.dumps({'type':'item.completed','item':{'type':'tool_call','text':'no'}}))\n"
        "print(json.dumps({'type':'turn.completed'}))\n",
    )
    client = CodexIdeationClient(codex_bin=str(exe), local_root=tmp_path / "runs")
    with pytest.raises(CodexIdeationTransportError, match="forbidden"):
        await client.chat.completions.create(
            messages=MESSAGES, response_format={"json_schema": {"schema": SCHEMA}}, tools=[]
        )


@pytest.mark.asyncio
async def test_stream_cap_fails_without_buffering_full_stdout(tmp_path: Path) -> None:
    exe = _exe(tmp_path, "import sys\nsys.stdout.write('x' * 1000000)\nsys.stdout.flush()\n")
    client = CodexIdeationClient(
        codex_bin=str(exe), max_jsonl_bytes=1024, local_root=tmp_path / "runs"
    )
    with pytest.raises(CodexIdeationTransportError, match="byte limit"):
        await client.chat.completions.create(
            messages=MESSAGES, response_format={"json_schema": {"schema": SCHEMA}}, tools=[]
        )


@pytest.mark.asyncio
async def test_cancellation_terminates_the_child_group(tmp_path: Path) -> None:
    pid_file = tmp_path / "pid"
    exe = _exe(
        tmp_path,
        "import os, pathlib, time\n"
        f"pathlib.Path({str(pid_file)!r}).write_text(str(os.getpid()))\n"
        "time.sleep(30)\n",
    )
    client = CodexIdeationClient(codex_bin=str(exe), local_root=tmp_path / "runs")
    task = asyncio.create_task(
        client.chat.completions.create(
            messages=MESSAGES, response_format={"json_schema": {"schema": SCHEMA}}, tools=[]
        )
    )
    for _ in range(40):
        if pid_file.exists():
            break
        await asyncio.sleep(0.025)
    assert pid_file.exists()
    pid = int(pid_file.read_text())
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


@pytest.mark.asyncio
@pytest.mark.parametrize("missing_pipe", ["stdin", "stdout", "stderr"])
async def test_unavailable_pipe_terminates_the_actual_child(
    tmp_path: Path, missing_pipe: str
) -> None:
    child = None

    async def start_child(*args, **kwargs):
        nonlocal child
        kwargs[missing_pipe] = asyncio.subprocess.DEVNULL
        child = await asyncio.create_subprocess_exec(*args, **kwargs)
        return child

    exe = _exe(tmp_path, "import time\ntime.sleep(30)\n")
    client = CodexIdeationClient(
        codex_bin=str(exe), local_root=tmp_path / "runs", process_factory=start_child
    )
    with pytest.raises(CodexIdeationTransportError, match="pipes are unavailable"):
        await client.chat.completions.create(
            messages=MESSAGES, response_format={"json_schema": {"schema": SCHEMA}}, tools=[]
        )
    assert child is not None and child.returncode is not None
    with pytest.raises(ProcessLookupError):
        os.kill(child.pid, 0)
