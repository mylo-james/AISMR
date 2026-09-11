"""AISMR's Vercel FastAPI entry point."""

import sys
from pathlib import Path

# Vercel's optimized dependency install does not retain editable src-layout paths.
sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from myloware.studio.hosted_app import app

__all__ = ["app"]
