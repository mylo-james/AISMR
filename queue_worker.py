"""Native Vercel subscriber discovery for AISMR's src-layout package."""

import sys
from pathlib import Path

# The queue function has its own cold start and uses the same packaged source.
sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from myloware.studio.hosted_queue import advance_recorded

__all__ = ["advance_recorded"]
