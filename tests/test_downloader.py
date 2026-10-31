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


def test_export_playlist_m3u(config: Config, db: Database, tmp_path: Path) -> None:
    from fiiolib.downloads.downloader import export_all_playlists, export_playlist_m3u

    artist_id = repo.get_or_create_artist(db, "Acoustic Band")
    album_id = repo.get_or_create_album(db, "Summer Chill", artist_id=artist_id)
    track_id = repo.get_or_create_track(db, "Sunny Road", artist_id=artist_id, album_id=album_id, track_number=1, duration_ms=180000)

    # Insert playlist and track
    cur = db.conn.cursor()
    cur.execute("INSERT INTO playlists (spotify_id, name, owner_name) VALUES ('pl_123', 'Roadtrip Vibes', 'Hitesh')")
    pl_id = cur.lastrowid
    cur.execute("INSERT INTO playlist_tracks (playlist_id, track_id, position) VALUES (?, ?, 0)", (pl_id, track_id))
    db.conn.commit()

    # Track has no local file yet -> export returns None
    assert export_playlist_m3u(config, db, pl_id, target_dir=tmp_path) is None

    # Now add validated file
    music_dir = config.root / "music" / "Acoustic Band" / "Summer Chill"
    music_dir.mkdir(parents=True, exist_ok=True)
    audio_file = music_dir / "01 Sunny Road.flac"
    audio_file.write_bytes(b"dummy")

    repo.upsert_file(
        db,
        str(audio_file),
        format="flac",
        sample_rate=44100,
        bit_depth=16,
        channels=2,
        duration_ms=180000,
        sha256="road123",
        track_id=track_id,
        validated=1,
    )

    out_path = export_playlist_m3u(config, db, pl_id, target_dir=tmp_path)
    assert out_path is not None
    assert out_path.exists()
    content = out_path.read_text(encoding="utf-8")
    assert "#EXTM3U" in content
    assert "#EXTINF:180,Acoustic Band - Sunny Road" in content
    assert "Sunny Road.flac" in content

    exported = export_all_playlists(config, db, target_dir=tmp_path)
    assert len(exported) >= 1

