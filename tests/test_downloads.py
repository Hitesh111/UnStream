"""Rights-gated acquisition and torrent hashing tests (no network)."""

from __future__ import annotations

import hashlib
import json

import pytest

from fiiolib.config import Config
from fiiolib.db import Database, repo
from fiiolib.downloads.manager import (
    AcquisitionBlocked,
    DownloadManager,
    infohash_from_magnet,
    infohash_from_torrent,
)

MAGNET = "magnet:?xt=urn:btih:0123456789abcdef0123456789abcdef01234567&dn=test"


class FakeQBittorrent:
    def __init__(self) -> None:
        self.added: list[dict] = []
        self.closed = False

    def is_available(self) -> tuple[bool, str]:
        return True, "5.0.0"

    def add_torrent(self, **kwargs) -> None:
        self.added.append(kwargs)

    def fetch_torrent_file(self, url: str) -> bytes:
        raise AssertionError("magnet test should not fetch a .torrent")

    def close(self) -> None:
        self.closed = True


def _candidate_with_release(
    db: Database,
    *,
    allowed: int,
    review: int,
    authorisation: str = "creative_commons",
    external_id: str = "release-1",
) -> tuple[int, int]:
    artist = repo.get_or_create_artist(db, "Gate Artist")
    album = repo.get_or_create_album(db, "Gate Album", artist_id=artist)
    candidate = repo.upsert_candidate(
        db,
        entity_type="album",
        entity_name="Gate Album",
        entity_spotify_id=f"gate-{external_id}",
        score=5.0,
        reasons=[],
        artist_id=artist,
        album_id=album,
    )
    source = db.one("SELECT id FROM sources WHERE name = 'internet_archive'")
    release = db.execute(
        "INSERT INTO releases(source_id, external_id, title, artist_name, album_id, candidate_id, "
        "download_url, licence, licence_url, redistribution_allowed, authorisation, needs_manual_review, "
        "size_bytes, metadata) VALUES (?, ?, 'Gate Album', 'Gate Artist', ?, ?, ?, 'cc_by', "
        "'https://creativecommons.org/licenses/by/4.0/', ?, ?, ?, 1024, ?)",
        (source["id"], external_id, album, candidate, MAGNET, allowed, authorisation, review,
         json.dumps({"creator": "Gate Artist"})),
    ).lastrowid
    return candidate, int(release)


def test_unverified_rights_are_never_queued(config: Config, db: Database) -> None:
    config.downloads.enabled = True
    candidate, _ = _candidate_with_release(db, allowed=0, review=1)
    manager = DownloadManager(config, db)

    approved = manager.approve(candidate)
    assert approved["approved_acquisitions"] == []
    assert approved["releases_requiring_review"]

    fake = FakeQBittorrent()
    manager = DownloadManager(config, db, qbit=fake)  # type: ignore[arg-type]
    result = manager.queue(paused=True)
    assert result.queued == []
    assert fake.added == []
    assert db.scalar("SELECT COUNT(*) FROM acquisitions") == 0


def test_allowed_release_is_queued_paused_with_tags(config: Config, db: Database) -> None:
    config.downloads.enabled = True
    candidate, _ = _candidate_with_release(db, allowed=1, review=0)
    manager = DownloadManager(config, db)
    approved = manager.approve(candidate)
    assert len(approved["approved_acquisitions"]) == 1
    acquisition = approved["approved_acquisitions"][0]

    fake = FakeQBittorrent()
    manager = DownloadManager(config, db, qbit=fake)  # type: ignore[arg-type]
    result = manager.queue(paused=True)
    assert len(result.queued) == 1
    assert result.queued[0]["hash"] == "0123456789abcdef0123456789abcdef01234567"
    assert fake.added and fake.added[0]["paused"] is True
    assert fake.added[0]["tags"] == f"fiiolib,acq-{acquisition}"
    assert fake.added[0]["url"] == MAGNET

    job = db.one("SELECT * FROM torrent_jobs WHERE acquisition_id = ?", (acquisition,))
    assert job["status"] == "paused"
    assert job["save_path"].startswith(str(config.downloads_dir))
    status = manager.status()
    assert status["counts"]["queued"] == 1


def test_downloads_disabled_blocks_queueing(config: Config, db: Database) -> None:
    config.downloads.enabled = False
    manager = DownloadManager(config, db, qbit=FakeQBittorrent())  # type: ignore[arg-type]
    with pytest.raises(AcquisitionBlocked, match="disabled"):
        manager.queue()


def test_torrent_hashing() -> None:
    assert infohash_from_magnet(MAGNET) == "0123456789abcdef0123456789abcdef01234567"
    assert infohash_from_magnet("not a magnet") is None

    info = b"d4:name4:teste"
    torrent = b"d4:info" + info + b"e"
    assert infohash_from_torrent(torrent) == hashlib.sha1(info).hexdigest()
