"""Dry-run-first seeding of one owner-authorized recorded library entry."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from uuid import UUID, uuid5

from myloware.studio.portfolio_library_service import PortfolioLibraryService, PublicFinal
from myloware.studio.recorded import RecordedMediaArchive, RecordedMediaError


class RecordedSeedError(ValueError):
    """The requested recorded seed is not authorized or cannot be verified."""


@dataclass(frozen=True)
class RecordedSeed:
    archive_root: Path
    final: Path
    manifest_sha256: str
    final_sha256: str
    authorization_id: str
    public_suitability_receipt: str
    rights_receipt: str
    public_evidence: dict[str, object]


def inspect_recorded_seed(
    *,
    archive_root: Path,
    final: Path,
    authorization_receipt: Path | None = None,
) -> RecordedSeed:
    """Validate archive inputs without copying bytes or opening a library database."""
    root = archive_root.resolve()
    final_path = final.resolve()
    if root not in final_path.parents or not final_path.is_file():
        raise RecordedSeedError("seed final must be a regular file inside the recorded archive")
    try:
        archive = RecordedMediaArchive(root)
        archive.all_media()
    except (RecordedMediaError, OSError) as exc:
        raise RecordedSeedError("recorded archive manifest or assets are invalid") from exc
    manifest = root / "fixture-manifest.json"
    manifest_hash = _sha256_file(manifest)
    final_hash = _sha256_file(final_path)
    if authorization_receipt is None:
        raise RecordedSeedError(
            "an exact owner authorization receipt is required for recorded seeding"
        )
    receipt = _load_receipt(authorization_receipt)
    _require_match(receipt, "archive_root", str(root))
    _require_match(receipt, "manifest_sha256", manifest_hash)
    _require_match(receipt, "final_sha256", final_hash)
    authorization_id = receipt.get("authorization_id")
    suitability = _current_final_receipt(
        receipt.get("public_suitability"), final_hash, "suitability"
    )
    rights = _current_final_receipt(receipt.get("public_rights"), final_hash, "rights")
    if not isinstance(authorization_id, str) or not authorization_id.strip():
        raise RecordedSeedError("seed authorization receipt is incomplete")
    return RecordedSeed(
        archive_root=root,
        final=final_path,
        manifest_sha256=manifest_hash,
        final_sha256=final_hash,
        authorization_id=authorization_id,
        public_suitability_receipt=str(suitability["receipt"]),
        rights_receipt=str(rights["receipt"]),
        public_evidence={
            "suitability_expires_at": suitability["expires_at"],
            "suitability_receipt_id": suitability["receipt_id"],
            "rights_profile_expires_at": rights["expires_at"],
            "rights_profile_version": rights["profile_version"],
            "rights_profile_receipt_id": rights["receipt_id"],
            "revoked": False,
        },
    )


async def apply_recorded_seed(
    seed: RecordedSeed,
    *,
    library: PortfolioLibraryService,
    item_label: str,
) -> str:
    """Copy one already validated final into a separately configured public library."""
    if not item_label.strip():
        raise RecordedSeedError("seed item label is required")
    await library.initialize()
    source_id = uuid5(UUID("be9ec25e-46c5-4a36-a469-e1eb0caa45d0"), seed.final_sha256)
    entry = await library.accept(
        PublicFinal(
            source_instance_key=f"recorded-seed:{seed.authorization_id}:{seed.final_sha256}",
            source_run_id=source_id,
            source_revision=1,
            source_mode="recorded",
            item_label=item_label.strip(),
            source_final=seed.final,
            final_sha256=seed.final_sha256,
            accepted_at=datetime.now(UTC),
            public_suitability_receipt=seed.public_suitability_receipt,
            rights_receipt=seed.rights_receipt,
            consent_subject_hash=sha256(
                f"recorded-seed:{seed.authorization_id}:{seed.final_sha256}".encode()
            ).hexdigest(),
            history={"months": 12, **seed.public_evidence},
        )
    )
    return str(entry.id)


def _load_receipt(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RecordedSeedError("seed authorization receipt is unreadable") from exc
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        raise RecordedSeedError("seed authorization receipt schema is invalid")
    return value


def _require_match(receipt: dict[str, object], key: str, expected: str) -> None:
    if receipt.get(key) != expected:
        raise RecordedSeedError(f"seed authorization receipt {key} does not match")


def _current_final_receipt(value: object, final_hash: str, kind: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise RecordedSeedError("seed authorization receipt is incomplete")
    receipt = value.get("receipt")
    expires_at = value.get("expires_at")
    if value.get("status") != "passed" or value.get("final_sha256") != final_hash:
        raise RecordedSeedError("seed rights or suitability receipt does not match the final")
    if not isinstance(receipt, str) or not receipt.strip() or not isinstance(expires_at, str):
        raise RecordedSeedError("seed rights or suitability receipt is incomplete")
    try:
        expiry = datetime.fromisoformat(expires_at)
    except ValueError as exc:
        raise RecordedSeedError("seed rights or suitability receipt expiry is invalid") from exc
    if expiry.tzinfo is None or expiry.astimezone(UTC) <= datetime.now(UTC):
        raise RecordedSeedError("seed rights or suitability receipt is expired")
    receipt_id = value.get("receipt_id")
    if not isinstance(receipt_id, str) or not receipt_id.strip() or value.get("revoked") is True:
        raise RecordedSeedError("seed rights or suitability receipt is incomplete")
    result: dict[str, object] = {
        "receipt": receipt,
        "receipt_id": receipt_id,
        "expires_at": expires_at,
    }
    if kind == "rights":
        version = value.get("profile_version")
        if not isinstance(version, str) or not version.strip():
            raise RecordedSeedError("seed rights receipt profile is incomplete")
        result["profile_version"] = version
    return result


def _sha256_file(path: Path) -> str:
    try:
        return sha256(path.read_bytes()).hexdigest()
    except OSError as exc:
        raise RecordedSeedError("seed input is unreadable") from exc
