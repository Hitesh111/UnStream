"""Sync planning/execution tests using a fake mount point."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from fiiolib.audio.validate import sha256_file
from fiiolib.config import Config
from fiiolib.db import Database, repo
from fiiolib.device.detect import VolumeInfo
from fiiolib.sync.planner import SyncPlanner

sf = pytest.importorskip("soundfile", reason="soundfile is needed to generate FLAC fixtures")


def _flac(path: Path, frequency: float = 440.0) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    samples = (0.2 * np.sin(2 * np.pi * frequency * np.linspace(0, 2, 2 * 44100))).astype("float32")
    sf.write(path, samples, 44100, format="FLAC", subtype="PCM_16")
    return path


def _seed_library(config: Config, db: Database) -> Path:
    artist = repo.get_or_create_artist(db, "Sync Artist")
    album = repo.get_or_create_album(db, "Sync Album", artist_id=artist, release_year=2021)
    track = repo.get_or_create_track(
        db, "Sync Track", artist_id=artist, album_id=album, track_number=1, duration_ms=2000
    )
    repo.link_genres(db, "track_genres", "track_id", track, ["Techno"])
    source = _flac(config.music_dir / "Electronic" / "Techno" / "Sync Artist" / "Sync Album" / "01 - Sync Track.flac")
    repo.upsert_file(
        db,
        str(source),
        track_id=track,
        size_bytes=source.stat().st_size,
        sha256=sha256_file(source),
        codec="flac",
        bit_depth=16,
        sample_rate=44100,
        validated=1,
        organised_at="2026-01-01T00:00:00Z",
    )
    return source


def _volume(card: Path) -> VolumeInfo:
    return VolumeInfo(
        identifier="TEST-CARD",
        volume_uuid="TEST-CARD",
        volume_name="sandisk64",
        mount_point=str(card),
        filesystem="exfat",
        capacity_bytes=64 * 1024**3,
        device="/dev/disk99s1",
        removable=True,
        internal=False,
        is_echo_candidate=True,
    )


def test_sync_requires_confirmation(config: Config, db: Database, tmp_path: Path) -> None:
    _seed_library(config, db)
    card = tmp_path / "card"
    card.mkdir()
    planner = SyncPlanner(config, db)
    planner.locator.current = lambda hint=None: _volume(card)  # type: ignore[assignment]
    with pytest.raises(Exception, match="not been confirmed"):
        planner.plan()


def test_sync_add_skip_update_and_unmanaged(config: Config, db: Database, tmp_path: Path) -> None:
    source = _seed_library(config, db)
    card = tmp_path / "card"
    card.mkdir()
    planner = SyncPlanner(config, db)
    volume = _volume(card)
    planner.locator.current = lambda hint=None: volume  # type: ignore[assignment]
    planner.confirm(volume)

    plan = planner.plan()
    assert plan.fits
    assert plan.counts["add"] == 1
    added = plan.items[0]
    assert Path(added.destination) == card / "Music" / "Electronic" / "Techno" / "Sync Artist" / "Sync Album" / source.name

    dry = planner.execute(plan, dry_run=True)
    assert dry["added"] == 0
    assert not Path(added.destination).exists()

    result = planner.execute(plan, dry_run=False, verify=True)
    assert result["added"] == 1
    assert result["errors"] == []
    assert Path(added.destination).exists()
    assert sha256_file(Path(added.destination)) == sha256_file(source)

    second = planner.plan()
    assert second.counts["skip"] == 1
    assert second.counts["add"] == 0

    with Path(added.destination).open("ab") as handle:
        handle.write(b"tampered")
    third = planner.plan()
    assert third.counts["update"] == 1

    unmanaged = card / "Music" / "Unsorted" / "stray.flac"
    _flac(unmanaged)
    fourth = planner.plan()
    assert any(item["path"] == str(unmanaged) for item in fourth.unmanaged)

    run = planner.execute(fourth, dry_run=False, verify=True)
    assert run["updated"] == 1
    assert Path(unmanaged).exists()
