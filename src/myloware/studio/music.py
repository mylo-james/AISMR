"""The one approved music asset, with content identity checked before use."""

from hashlib import sha256
from pathlib import Path

from myloware.storage.studio_store import StudioError

TENDER_MOMENT_SHA256 = "25215e633c8eab3ee27c82f7b2f9c5af78f1d1e1ea48308a07e6e9d1acd06ae5"


def cleared_music(music_id: str) -> Path:
    if music_id != "tender-moment":
        raise StudioError("cleared_music_not_configured")
    path = Path("data/media/tender-moment.mp3").resolve()
    if not path.is_file() or sha256(path.read_bytes()).hexdigest() != TENDER_MOMENT_SHA256:
        raise StudioError("cleared_music_unavailable")
    return path
