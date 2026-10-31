"""Organiser, selection, and duplicate-detection tests."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from fiiolib.audio.validate import sha256_file
from fiiolib.config import Config
from fiiolib.db import Database, repo
from fiiolib.library.dedupe import Deduplicator
from fiiolib.library.organiser import Organiser
from fiiolib.sync.selection import select_files

sf = pytest.importorskip("soundfile", reason="soundfile is needed to generate FLAC fixtures")


def _write_flac(path: Path, frequency: float = 440.0) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    samples = (0.2 * np.sin(2 * np.pi * frequency * np.linspace(0, 2, 2 * 44100))).astype("float32")
    sf.write(path, samples, 44100, format="FLAC", subtype="PCM_16")
    return path


def _make_track(config: Config, db: Database, title: str, genre: str = "Techno", number: int = 1) -> tuple[int, Path]:
    artist = repo.get_or_create_artist(db, "Test Artist")
    album = repo.get_or_create_album(db, "Test Album", artist_id=artist, release_year=2020)
    track = repo.get_or_create_track(
        db, title, artist_id=artist, album_id=album, track_number=number, duration_ms=2000
    )
    repo.link_genres(db, "track_genres", "track_id", track, [genre])
    path = _write_flac(config.downloads_dir / f"{title}.flac")
    repo.upsert_file(
        db,
        str(path),
        track_id=track,
        size_bytes=path.stat().st_size,
        sha256=sha256_file(path),
        codec="flac",
        bit_depth=16,
        sample_rate=44100,
        validated=1,
    )
    return track, path


def test_organiser_moves_into_genre_structure(config: Config, db: Database) -> None:
    track, source = _make_track(config, db, "Night Drive")
    result = Organiser(config, db).organise(source, track_id=track)
    assert result.action == "move"
    destination = Path(result.destination)
    assert destination.exists()
    assert destination.is_relative_to(config.music_dir)
    assert "Electronic" in destination.parts and "Techno" in destination.parts
    assert destination.name == "01 - Night Drive.flac"
    assert not source.exists()
    row = db.one("SELECT track_id, organised_at, validated FROM files WHERE path = ?", (str(destination),))
    assert row["track_id"] == track
    assert row["organised_at"]
    assert db.scalar("SELECT COUNT(*) FROM files") == 1
    from fiiolib.library.scanner import LibraryScanner
    assert LibraryScanner(config, db).integrity_check() == []


def test_selection_profiles(config: Config, db: Database) -> None:
    for index in range(3):
        track, source = _make_track(config, db, f"Track {index}", number=index + 1)
        Organiser(config, db).organise(source, track_id=track)

    complete = select_files(db, profile="complete")
    assert len(complete.files) == 3

    genre = select_files(db, profile="genre", genres=["Techno"])
    assert len(genre.files) == 3

    none = select_files(db, profile="genre", genres=["Flamenco"])
    assert none.files == []

    limited = select_files(db, profile="daily")
    assert len(limited.files) == 3

    with pytest.raises(ValueError):
        select_files(db, profile="nonsense")


def test_dedupe_reports_but_never_deletes(config: Config, db: Database) -> None:
    artist = repo.get_or_create_artist(db, "Dup Artist")
    album = repo.get_or_create_album(db, "Dup Album", artist_id=artist)

    def insert(track_title: str, digest: str, path: str, number: int) -> tuple[int, int]:
        track = repo.get_or_create_track(
            db, track_title, artist_id=artist, album_id=album, track_number=number
        )
        cursor = db.execute(
            "INSERT INTO files(path, track_id, size_bytes, sha256, codec, validated, organised_at) "
            "VALUES (?, ?, ?, ?, 'flac', 1, datetime('now'))",
            (path, track, 1000, digest),
        )
        return track, int(cursor.lastrowid)

    insert("Same", "hash-a", "/tmp/dup-a.flac", 1)
    insert("Same Copy", "hash-a", "/tmp/dup-b.flac", 2)

    same_track, c = insert("Repeat", "hash-c", "/tmp/dup-c.flac", 3)
    _, d = insert("Repeat", "hash-d", "/tmp/dup-d.flac", 3)

    groups = Deduplicator(config, db).find()
    checksum_keys = {group.key for group in groups if group.kind == "checksum"}
    track_keys = {group.key for group in groups if group.kind == "track"}
    assert "hash-a" in checksum_keys
    assert f"track:{same_track}" in track_keys
    assert {c, d} <= {item["id"] for group in groups if group.kind == "track" for item in group.files}

    summary = Deduplicator(config, db).summary()
    assert summary["groups"] >= 2
    assert summary["duplicate_files"] >= 2
    assert "deleted automatically" in summary["note"]
