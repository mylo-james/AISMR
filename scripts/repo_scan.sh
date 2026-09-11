#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

echo "== AISMR Repo Scan =="
if command -v git >/dev/null 2>&1 && git rev-parse --git-dir >/dev/null 2>&1; then
  echo "Repo: $(basename "$(pwd)")"
  echo "Head: $(git rev-parse --short HEAD)"
  if [[ -n "$(git status --porcelain=v1)" ]]; then
    echo ""
    echo "WARN: Working tree is not clean:"
    git status --porcelain=v1
  fi
else
  echo "WARN: Not a git repo; skipping git checks."
fi

echo ""
echo "== Local Artifacts (not committed, but can leak in a zip) =="

warn=0

for f in .env .env.bak .env.local .env.development .env.production; do
  if [[ -f "$f" ]]; then
    echo "WARN: Found $f (avoid zipping a working directory with secrets)."
    warn=1
  fi
done

db_hits="$(find . -maxdepth 2 -type f \( -name '*.db' -o -name '*.db-wal' -o -name '*.db-shm' \) 2>/dev/null | sed 's|^\./||' || true)"
if [[ -n "${db_hits}" ]]; then
  echo "WARN: Found local DB files (safe to keep locally; avoid sharing in a zip):"
  echo "${db_hits}" | sed 's/^/  - /'
  warn=1
fi

log_hits="$(find . -maxdepth 2 -type f -name '*.log' 2>/dev/null | sed 's|^\./||' || true)"
if [[ -n "${log_hits}" ]]; then
  echo "WARN: Found local log files (avoid sharing in a zip):"
  echo "${log_hits}" | sed 's/^/  - /'
  warn=1
fi

echo ""
echo "== Tracked Secret Heuristics (best-effort) =="
if command -v rg >/dev/null 2>&1 && command -v git >/dev/null 2>&1; then
  # Scan tracked files only (avoids .venv, caches, etc.). These are heuristics; false positives are possible.
  # Patterns are intentionally conservative.
  set +e
  git ls-files -z \
    | xargs -0 rg -n --no-heading --hidden -S \
      "(-----BEGIN (RSA )?PRIVATE KEY-----|AKIA[0-9A-Z]{16}|sk-[A-Za-z0-9]{16,})" \
      2>/dev/null
  found=$?
  set -e
  if [[ $found -eq 0 ]]; then
    echo ""
    echo "WARN: Possible secret-like strings found above. Verify these are not real credentials."
    warn=1
  else
    echo "OK: No obvious secret-like patterns found in tracked files."
  fi
else
  echo "NOTE: rg/git not available; skipping tracked secret heuristic scan."
fi

echo ""
if [[ $warn -eq 1 ]]; then
  echo "Result: WARNINGS found. Recommended: push to GitHub or use 'git archive' (avoid zipping a working directory)."
else
  echo "Result: No obvious sharing risks detected."
fi

exit 0
