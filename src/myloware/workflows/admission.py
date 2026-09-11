"""Central fail-closed boundaries for the visitor-operated AISMR studio."""

from __future__ import annotations

from myloware.config.studio import get_studio_settings

STUDIO_ADMISSION_REQUIRED = "studio_admission_required"


class StudioAdmissionRequiredError(RuntimeError):
    """Raised when legacy paths attempt to bypass StudioStore.admit."""

    code = STUDIO_ADMISSION_REQUIRED

    def __init__(self) -> None:
        super().__init__(f"{self.code}: use the monthly visitor interface")


def studio_admission_enabled() -> bool:
    """Read the explicit product boundary at the operation point."""
    return get_studio_settings().enabled


def require_legacy_run_creation_allowed() -> None:
    """Reserve new runs through StudioStore.admit while AISMR is active."""
    if studio_admission_enabled():
        raise StudioAdmissionRequiredError()


def require_legacy_monthly_engine_allowed(workflow_name: str | None) -> None:
    """Prevent a manually created monthly row from entering the legacy graph."""
    if studio_admission_enabled() and workflow_name == "monthly":
        raise StudioAdmissionRequiredError()
