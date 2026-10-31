"""End-to-end pipeline: authorised release -> validate -> tag -> organise -> card sync."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from fiiolib.audio.validate import sha256_file
from fiiolib.config import Config
from fiiolib.db import Database, repo
from fiiolib.device.detect import VolumeInfo
from fiiolib.downloads.manager import DownloadManager
from fiiolib.metadata.tagger import TrackMetadata, write_tags
from fiiolib.sync.planner import SyncPlanner

sf = pytest.importorskip("soundfile", reason="soundfile is needed to generate FLAC fixtures")


def _flac(path: Path, frequency: float = 330.0) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    samples = (0.2 * np.sin(2 * np.pi * frequency * np.linspace(0, 2, 2 * 44100))).astype("float32")
    sf.write(path, samples, 44100, format="FLAC", subtype="PCM_16")
    return path


def test_authorised_acquisition_reaches_the_card(config: Config, db: Database, tmp_path: Path) -> None:
    artist = repo.get_or_create_artist(db, "Pipeline Artist")
    album = repo.get_or_create_album(db, "Pipeline Album", artist_id=artist, release_year=2024)
    track = repo.get_or_create_track(
        db, "First Light", artist_id=artist, album_id=album, track_number=1, duration_ms=2000
    )
    repo.link_genres(db, "track_genres", "track_id", track, ["Techno"])

    candidate = repo.upsert_candidate(
        db,
        entity_type="album",
        entity_name="Pipeline Album",
        entity_spotify_id="pipeline-album",
        score=10.0,
        reasons=[{"code": "album_priority", "reason": "Saved album"}],
        artist_id=artist,
        album_id=album,
    )
    source = db.one("SELECT id FROM sources WHERE name = 'internet_archive'")
    db.execute(
        "INSERT INTO releases(source_id, external_id, title, artist_name, album_id, candidate_id, "
        "licence, licence_url, redistribution_allowed, authorisation, needs_manual_review, metadata) "
        "VALUES (?, 'pipeline-release', 'Pipeline Album', 'Pipeline Artist', ?, ?, 'cc_by', "
        "'https://creativecommons.org/licenses/by/4.0/', 1, 'creative_commons', 0, ?)",
        (source["id"], album, candidate, json.dumps({"creator": "Pipeline Artist"})),
    )

    manager = DownloadManager(config, db)
    approved = manager.approve(candidate)
    assert approved["approved_acquisitions"]
    acquisition = approved["approved_acquisitions"][0]

    download_dir = config.downloads_dir / "pipeline-release"
    download_dir.mkdir(parents=True)
    audio = _flac(download_dir / "first-light.flac")
    write_tags(
        audio,
        TrackMetadata(
            title="First Light",
            artist="Pipeline Artist",
            album="Pipeline Album",
            album_artist="Pipeline Artist",
            genre="Techno",
            track_number=1,
        ),
    )

    db.execute(
        "INSERT INTO torrent_jobs(acquisition_id, qbit_hash, name, save_path, status, progress) "
        "VALUES (?, 'pipelinehash', 'Pipeline Album', ?, 'completed', 1.0)",
        (acquisition, str(download_dir)),
    )
    db.execute("UPDATE acquisitions SET status = 'downloaded' WHERE id = ?", (acquisition,))

    result = manager.process_downloaded()
    assert result["failed"] == 0
    assert result["organised"] == 1
    assert result["unmatched"] == 0

    organised = db.one(
        "SELECT f.path, f.organised_at, f.sha256, f.validated FROM files f "
        "JOIN tracks t ON t.id = f.track_id WHERE t.id = ?",
        (track,),
    )
    assert organised is not None and organised["organised_at"]
    organised_path = Path(organised["path"])
    assert organised_path.is_file()
    assert organised_path.is_relative_to(config.music_dir)
    assert organised_path.name == "01 - First Light.flac"
    assert organised_path.parts[-4:-1] == ("Techno", "Pipeline Artist", "Pipeline Album")

    status = db.one("SELECT status FROM acquisitions WHERE id = ?", (acquisition,))
    assert status["status"] == "organised"

    card = tmp_path / "card"
    card.mkdir()
    volume = VolumeInfo(
        identifier="E2E-CARD",
        volume_uuid="E2E-CARD",
        volume_name="sandisk64",
        mount_point=str(card),
        filesystem="exfat",
        capacity_bytes=64 * 1024**3,
        device="/dev/disk98s1",
        removable=True,
        internal=False,
        is_echo_candidate=True,
    )
    planner = SyncPlanner(config, db)
    planner.locator.current = lambda hint=None: volume  # type: ignore[assignment]
    planner.confirm(volume)

    plan = planner.plan(profile="complete")
    assert plan.counts["add"] == 1
    sync = planner.execute(plan, dry_run=False, verify=True)
    assert sync["added"] == 1 and not sync["errors"]

    copied = Path(plan.items[0].destination)
    assert copied.exists()
    assert sha256_file(copied) == organised["sha256"]
    assert copied.name == "01 - First Light.flac"

    candidate_row = db.one("SELECT status FROM discovery_candidates WHERE id = ?", (candidate,))
    assert candidate_row["status"] == "synced"
