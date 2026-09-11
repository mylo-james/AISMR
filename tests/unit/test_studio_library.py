from __future__ import annotations

from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

import pytest

from myloware.studio.library import LibraryCandidate, StudioLibrary


def candidate(run_id, when, *, mode="recorded", valid=True, status="video_complete"):
    payload = b"final" + str(run_id).encode()
    return LibraryCandidate(
        run_id,
        status,
        mode,
        sha256(payload).hexdigest() if valid else "0" * 64,
        {"media_verified": True},
        when,
    )


def materialize(root: Path, item: LibraryCandidate) -> None:
    directory = root / str(item.run_id)
    directory.mkdir(parents=True)
    (directory / "final.mp4").write_bytes(b"final" + str(item.run_id).encode())
    (directory / ("a" * 64 + ".media")).write_bytes(b"temporary")


def test_retains_at_most_three_and_evicts_old_final(tmp_path: Path) -> None:
    media, fixtures = tmp_path / "media", tmp_path / "fixtures"
    now = datetime.now(UTC)
    items = [candidate(uuid4(), now - timedelta(minutes=index)) for index in range(4)]
    for item in items:
        materialize(media, item)
    receipt = StudioLibrary(media_root=media, fixture_root=fixtures).prune(items)
    assert receipt.kept_run_ids == tuple(item.run_id for item in items[:3])
    assert receipt.evicted_run_ids == (items[3].run_id,)
    assert not (media / str(items[3].run_id)).exists()
    assert all((media / str(item.run_id) / "final.mp4").is_file() for item in items[:3])
    assert not any(
        (media / str(item.run_id) / ("a" * 64 + ".media")).exists() for item in items[:3]
    )


def test_corrupt_final_never_causes_destructive_cleanup(tmp_path: Path) -> None:
    media, fixtures = tmp_path / "media", tmp_path / "fixtures"
    item = candidate(uuid4(), datetime.now(UTC), valid=False)
    materialize(media, item)
    StudioLibrary(media_root=media, fixture_root=fixtures).prune([item])
    assert (media / str(item.run_id) / "final.mp4").is_file()
    assert (media / str(item.run_id) / ("a" * 64 + ".media")).is_file()


def test_active_and_unknown_states_are_protected(tmp_path: Path) -> None:
    media, fixtures = tmp_path / "media", tmp_path / "fixtures"
    now = datetime.now(UTC)
    items = [
        candidate(uuid4(), now, status=status)
        for status in ("generating", "final_review", "submission_unknown")
    ]
    for item in items:
        materialize(media, item)
    StudioLibrary(media_root=media, fixture_root=fixtures).prune(items)
    assert all((media / str(item.run_id) / ("a" * 64 + ".media")).is_file() for item in items)


def test_missing_completion_timestamp_and_fixture_final_are_excluded(tmp_path: Path) -> None:
    media, fixtures = tmp_path / "media", tmp_path / "fixtures"
    timed = candidate(uuid4(), datetime.now(UTC))
    missing = candidate(uuid4(), None)
    fixture = candidate(uuid4(), datetime.now(UTC), mode="fixture")
    for item in (timed, missing, fixture):
        materialize(media, item)
    retained = StudioLibrary(media_root=media, fixture_root=fixtures).retained(
        [timed, missing, fixture]
    )
    assert retained == (timed,)
    assert (media / str(missing.run_id) / "final.mp4").is_file()
    assert (media / str(fixture.run_id) / "final.mp4").is_file()
    receipt = StudioLibrary(media_root=media, fixture_root=fixtures).prune(
        [timed, missing, fixture]
    )
    assert fixture.run_id in receipt.kept_run_ids


def test_terminal_failed_namespace_is_cleaned(tmp_path: Path) -> None:
    media, fixtures = tmp_path / "media", tmp_path / "fixtures"
    item = candidate(uuid4(), datetime.now(UTC), status="failed")
    materialize(media, item)
    StudioLibrary(media_root=media, fixture_root=fixtures).prune([item])
    assert not (media / str(item.run_id)).exists()


def test_absent_roots_are_noop_and_symlinks_are_not_followed(tmp_path: Path) -> None:
    library = StudioLibrary(
        media_root=tmp_path / "missing-media", fixture_root=tmp_path / "missing-fixtures"
    )
    assert library.prune([]).removed_paths == ()
    external = tmp_path / "external"
    external.mkdir()
    (external / "source.txt").write_text("preserve")
    media = tmp_path / "media"
    media.mkdir()
    item = candidate(uuid4(), datetime.now(UTC))
    run = media / str(item.run_id)
    run.symlink_to(external, target_is_directory=True)
    receipt = StudioLibrary(media_root=media, fixture_root=tmp_path / "fixtures").prune([item])
    assert (external / "source.txt").is_file() and receipt.removed_paths == ()


def test_unexpected_and_symlink_children_are_preserved(tmp_path: Path) -> None:
    media, fixtures = tmp_path / "media", tmp_path / "fixtures"
    item = candidate(uuid4(), datetime.now(UTC))
    materialize(media, item)
    run = media / str(item.run_id)
    (run / "notes.txt").write_text("keep")
    external = tmp_path / "external.txt"
    external.write_text("keep")
    (run / "linked.txt").symlink_to(external)
    receipt = StudioLibrary(media_root=media, fixture_root=fixtures).prune([item])
    assert (run / "notes.txt").is_file() and (run / "linked.txt").is_symlink()
    assert external.read_text() == "keep"
    assert any(entry.startswith("unknown:") for entry in receipt.skipped)
    assert any(entry.startswith("symlink:") for entry in receipt.skipped)


def test_prune_is_idempotent(tmp_path: Path) -> None:
    media, fixtures = tmp_path / "media", tmp_path / "fixtures"
    item = candidate(uuid4(), datetime.now(UTC))
    materialize(media, item)
    library = StudioLibrary(media_root=media, fixture_root=fixtures)
    first, second = library.prune([item]), library.prune([item])
    assert first.kept_run_ids == second.kept_run_ids == (item.run_id,)
    assert second.removed_paths == ()


def test_fixture_cleanup_recognizes_only_owned_scene_media_and_sidecars(tmp_path: Path) -> None:
    library = StudioLibrary(media_root=tmp_path / "media", fixture_root=tmp_path / "fixtures")
    known = {
        "scene-01.mp4",
        "scene-12.mp4",
        "scene-01-title.wav",
        "scene-12-title.wav",
        "title-batch.wav",
        "title-batch-timestamps.json",
        "voice-batch-timestamps.json",
    }
    assert all(library._known_file(name, fixture=True) for name in known)
    assert not library._known_file("scene-13.mp4", fixture=True)
    assert not library._known_file("scene-01.wav", fixture=True)
    assert not library._known_file("scene-01-title.mp4", fixture=True)
    assert not library._known_file("scene-01-title.mp3", fixture=True)
    assert not library._known_file("unrelated.json", fixture=True)


def test_rejects_symlink_root(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "link"
    link.symlink_to(target, target_is_directory=True)
    with pytest.raises(ValueError):
        StudioLibrary(media_root=link, fixture_root=tmp_path / "fixtures")


def test_rejects_user_symlink_ancestor_before_resolution(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    parent = tmp_path / "linked-parent"
    parent.symlink_to(target, target_is_directory=True)
    with pytest.raises(ValueError):
        StudioLibrary(media_root=parent / "media", fixture_root=tmp_path / "fixtures")


def test_rejects_workspace_root_and_ancestor(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    workspace = tmp_path / "workspace"
    child = workspace / "child"
    child.mkdir(parents=True)
    monkeypatch.chdir(child)
    with pytest.raises(ValueError):
        StudioLibrary(media_root=workspace, fixture_root=tmp_path / "fixtures")
    with pytest.raises(ValueError):
        StudioLibrary(media_root=child, fixture_root=tmp_path / "fixtures")
