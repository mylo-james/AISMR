"""Enable the local in-process demo without replacing existing environment values."""

from __future__ import annotations

from pathlib import Path

env_path = Path(".env")
if not env_path.exists():
    raise SystemExit(".env missing; run `make demo-safe` first.")

overrides = {
    "DISABLE_BACKGROUND_WORKFLOWS": "false",
    "WORKFLOW_DISPATCHER": "inprocess",
}

lines = env_path.read_text(encoding="utf-8").splitlines(keepends=True)
seen: set[str] = set()
out: list[str] = []
for raw in lines:
    line = raw.rstrip("\n")
    if not line or line.lstrip().startswith("#") or "=" not in line:
        out.append(raw)
        continue
    key, _value = line.split("=", 1)
    key = key.strip()
    if key in overrides:
        out.append(f"{key}={overrides[key]}\n")
        seen.add(key)
    else:
        out.append(raw)

for key, value in overrides.items():
    if key not in seen:
        out.append(f"{key}={value}\n")

env_path.write_text("".join(out), encoding="utf-8")
