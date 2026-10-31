"""Tests for the direct track acquisition downloader."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from fiiolib.config import Config
from fiiolib.db import Database, repo
from fiiolib.downloads.downloader import download_playlist, download_saved_tracks, download_track


def test_download_track_missing_in_db(config: Config, db: Database) -> None:
    res = download_track(config, db, 999999)
    assert res["ok"] is False
    assert "not found" in res["error"]


def test_download_track_already_exists(config: Config, db: Database, tmp_path: Path) -> None:
    artist_id = repo.get_or_create_artist(db, "Test Artist")
    album_id = repo.get_or_create_album(db, "Test Album", artist_id=artist_id)
    track_id = repo.get_or_create_track(db, "Existing Song", artist_id=artist_id, album_id=album_id, track_number=1)

    dummy_file = tmp_path / "song.flac"
    dummy_file.write_bytes(b"dummy flac content")

    repo.upsert_file(
        db,
        str(dummy_file),
        format="flac",
        sample_rate=44100,
        bit_depth=16,
        channels=2,
        duration_ms=1000,
        sha256="abc",
        track_id=track_id,
        validated=1,
    )

    res = download_track(config, db, track_id)
    assert res["ok"] is True
    assert res["already_downloaded"] is True
    assert res["title"] == "Existing Song"


def test_download_saved_tracks_empty(config: Config, db: Database) -> None:
    res = download_saved_tracks(config, db, limit=5)
    assert res["ok"] is True
    assert res["total"] == 0
