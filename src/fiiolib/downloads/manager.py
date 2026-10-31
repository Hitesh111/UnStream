"""Acquisition workflow: approval, authorised queueing, polling and post-processing."""

from __future__ import annotations

import hashlib
import json
import logging
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fiiolib.audio.validate import validate_audio
from fiiolib.config import Config
from fiiolib.db import Database, repo, utcnow
from fiiolib.downloads.qbittorrent import (
    QBittorrentClient,
    QBittorrentError,
    torrent_hash_from_info,
)
from fiiolib.library.organiser import Organiser
from fiiolib.library.queries import track_context
from fiiolib.metadata.artwork import fetch_cover_art, fetch_image, prepare_artwork
from fiiolib.metadata.matcher import TrackMatcher
from fiiolib.metadata.tagger import TrackMetadata, read_tags, write_tags

logger = logging.getLogger(__name__)

BLOCKED_AUTHORISATIONS = {"unknown"}


class AcquisitionBlocked(RuntimeError):
    pass


@dataclass
class QueueResult:
    queued: list[dict[str, Any]] = field(default_factory=list)
    skipped: list[dict[str, Any]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {"queued": self.queued, "skipped": self.skipped, "errors": self.errors}


class DownloadManager:
    def __init__(self, config: Config, db: Database, qbit: QBittorrentClient | None = None) -> None:
        self.config = config
        self.db = db
        self.qbit = qbit or QBittorrentClient(config)
        self.organiser = Organiser(config, db)
        self.matcher = TrackMatcher(db)

    # --------------------------------------------------------------- approval
    def approve(self, candidate_id: int, notes: str | None = None) -> dict[str, Any]:
        candidate = self.db.one("SELECT * FROM discovery_candidates WHERE id = ?", (candidate_id,))
        if candidate is None:
            raise AcquisitionBlocked(f"Candidate {candidate_id} not found")
        releases = self.db.query("SELECT * FROM releases WHERE candidate_id = ?", (candidate_id,))
        approved: list[int] = []
        review: list[int] = []
        for release in releases:
            if (
                not release["redistribution_allowed"]
                or release["needs_manual_review"]
                or release["authorisation"] in BLOCKED_AUTHORISATIONS
            ):
                review.append(int(release["id"]))
                continue
            existing = self.db.one(
                "SELECT id FROM acquisitions WHERE release_id = ? AND status NOT IN ('failed','rejected')",
                (release["id"],),
            )
            if existing:
                approved.append(int(existing["id"]))
                continue
            cursor = self.db.execute(
                "INSERT INTO acquisitions(release_id, candidate_id, status, approved_at, approved_by, notes) "
                "VALUES (?, ?, 'approved', ?, 'user', ?)",
                (release["id"], candidate_id, utcnow(), notes),
            )
            approved.append(int(cursor.lastrowid))
        repo.set_candidate_status(self.db, candidate_id, "approved", notes)
        return {
            "candidate_id": candidate_id,
            "name": candidate["entity_name"],
            "approved_acquisitions": approved,
            "releases_requiring_review": review,
            "message": "Release rights are not verified; approve manually after checking the source"
            if review
            else None,
        }

    def reject(self, candidate_id: int, notes: str | None = None) -> dict[str, Any]:
        repo.set_candidate_status(self.db, candidate_id, "rejected", notes)
        self.db.execute(
            "UPDATE acquisitions SET status = 'rejected', notes = ?, updated_at = ? "
            "WHERE candidate_id = ? AND status = 'awaiting_approval'",
            (notes, utcnow(), candidate_id),
        )
        return {"candidate_id": candidate_id, "status": "rejected"}

    # ---------------------------------------------------------------- queueing
    def disk_check(self) -> tuple[bool, str]:
        target = self.config.downloads_dir
        target.mkdir(parents=True, exist_ok=True)
        free = shutil.disk_usage(target).free
        required = int(self.config.downloads.min_free_gb * 1024**3)
        if free - required < 0:
            return False, f"Only {free / 1024**3:.1f} GB free; reserve {self.config.downloads.min_free_gb} GB"
        return True, f"{free / 1024**3:.1f} GB free"

    def queue(self, limit: int | None = None, paused: bool = False) -> QueueResult:
        result = QueueResult()
        if not self.config.downloads.enabled:
            raise AcquisitionBlocked(
                "Downloads are disabled. Set downloads.enabled: true in the configuration."
            )
        ok, message = self.disk_check()
        if not ok:
            raise AcquisitionBlocked(message)
        available, version = self.qbit.is_available()
        if not available:
            raise QBittorrentError(
                f"qBittorrent is not reachable at {self.config.downloads.qbittorrent_url}: {version}"
            )

        sql = (
            "SELECT a.*, r.title AS release_title, r.authorisation, r.needs_manual_review, "
            "r.redistribution_allowed, r.download_url, r.media_type, r.size_bytes, r.licence, "
            "r.external_id, r.source_id FROM acquisitions a JOIN releases r ON r.id = a.release_id "
            "WHERE a.status = 'approved' ORDER BY a.id"
        )
        rows = self.db.query(sql)
        if limit:
            rows = rows[:limit]
        for row in rows:
            if (
                not row["redistribution_allowed"]
                or row["needs_manual_review"]
                or row["authorisation"] in BLOCKED_AUTHORISATIONS
            ):
                result.skipped.append(
                    {
                        "acquisition_id": row["id"],
                        "reason": "Release rights are not verified; manual review required",
                    }
                )
                continue
            try:
                if row["size_bytes"] and row["size_bytes"] > shutil.disk_usage(
                    self.config.downloads_dir
                ).free:
                    result.skipped.append(
                        {"acquisition_id": row["id"], "reason": "Not enough free disk space"}
                    )
                    continue
                job = self._add_release(row, paused=paused)
                result.queued.append(job)
            except Exception as exc:
                logger.exception("Queueing acquisition %s failed", row["id"])
                self.db.execute(
                    "UPDATE acquisitions SET status = 'failed', notes = ?, updated_at = ? WHERE id = ?",
                    (str(exc), utcnow(), row["id"]),
                )
                result.errors.append(f"Acquisition {row['id']}: {exc}")
        return result

    def _add_release(self, row: Any, paused: bool) -> dict[str, Any]:
        save_path = self.config.downloads_dir / f"{row['source_id']}-{row['external_id']}"
        save_path.mkdir(parents=True, exist_ok=True)
        url = row["download_url"] or ""
        torrent_bytes = None
        torrent_hash = None
        if url.startswith("magnet:"):
            torrent_hash = infohash_from_magnet(url)
        elif url.endswith(".torrent") or "archive.org/download" in url:
            torrent_bytes = self.qbit.fetch_torrent_file(url)
            torrent_hash = infohash_from_torrent(torrent_bytes)
        else:
            raise AcquisitionBlocked(f"Unsupported download URL for release {row['external_id']}")

        tags = f"fiiolib,acq-{row['id']}"
        self.qbit.add_torrent(
            url=None if torrent_bytes else url,
            torrent_bytes=torrent_bytes,
            save_path=str(save_path),
            tags=tags,
            paused=paused,
        )
        cursor = self.db.execute(
            "INSERT INTO torrent_jobs(acquisition_id, qbit_hash, name, magnet, save_path, status) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                row["id"],
                torrent_hash,
                row["release_title"],
                url,
                str(save_path),
                "queued" if not paused else "paused",
            ),
        )
        self.db.execute(
            "UPDATE acquisitions SET status = 'queued', updated_at = ? WHERE id = ?",
            (utcnow(), row["id"]),
        )
        return {
            "acquisition_id": int(row["id"]),
            "job_id": int(cursor.lastrowid),
            "hash": torrent_hash,
            "release": row["release_title"],
            "save_path": str(save_path),
        }

    # ------------------------------------------------------------------ poll
    def poll(self) -> list[dict[str, Any]]:
        jobs = self.db.query(
            "SELECT * FROM torrent_jobs WHERE status IN ('queued','downloading','paused','error')"
        )
        if not jobs:
            return []
        try:
            live = {torrent_hash_from_info(info): info for info in self.qbit.torrents(category="fiiolib")}
        except QBittorrentError as exc:
            return [{"error": str(exc)}]
        updates = []
        for job in jobs:
            info = live.get(job["qbit_hash"])
            if info is None:
                continue
            progress = float(info.get("progress") or 0)
            state = str(info.get("state") or "")
            status = "downloading"
            if state in {"pausedDL", "pausedUP", "stoppedDL", "stoppedUP"}:
                status = "paused"
            if progress >= 1.0 or state in {"uploading", "stalledUP", "queuedUP", "forcedUP"}:
                status = "completed"
            self.db.execute(
                "UPDATE torrent_jobs SET status = ?, progress = ?, downloaded_bytes = ?, "
                "total_bytes = ?, updated_at = ? WHERE id = ?",
                (
                    status,
                    progress,
                    int(info.get("downloaded") or 0),
                    int(info.get("size") or 0),
                    utcnow(),
                    job["id"],
                ),
            )
            acquisition_status = {
                "downloading": "downloading",
                "paused": "downloading",
                "completed": "downloaded",
            }[status]
            self.db.execute(
                "UPDATE acquisitions SET status = ?, updated_at = ? WHERE id = ?",
                (acquisition_status, utcnow(), job["acquisition_id"]),
            )
            updates.append(
                {
                    "acquisition_id": job["acquisition_id"],
                    "hash": job["qbit_hash"],
                    "status": status,
                    "progress": round(progress * 100, 1),
                }
            )
        return updates

    # --------------------------------------------------------- post-processing
    def process_downloaded(self, acquisition_id: int | None = None, dry_run: bool = False) -> dict[str, Any]:
        sql = (
            "SELECT a.*, r.title AS release_title, r.licence, r.licence_url, r.authorisation, "
            "r.external_id, r.metadata FROM acquisitions a JOIN releases r ON r.id = a.release_id "
            "WHERE a.status IN ('downloaded','verified')"
        )
        params: list[Any] = []
        if acquisition_id is not None:
            sql += " AND a.id = ?"
            params.append(acquisition_id)
        acquisitions = self.db.query(sql, params)
        summary: dict[str, Any] = {"processed": 0, "organised": 0, "unmatched": 0, "failed": 0, "files": []}
        for acquisition in acquisitions:
            job = self.db.one(
                "SELECT * FROM torrent_jobs WHERE acquisition_id = ? ORDER BY id DESC LIMIT 1",
                (acquisition["id"],),
            )
            if job is None:
                continue
            save_path = Path(job["save_path"])
            extension_set = {e.lower() for e in self.config.library.audio_extensions}
            audio_files = [
                path
                for path in save_path.rglob("*")
                if path.is_file()
                and path.suffix.lower() in extension_set
                and not path.name.startswith("._")
            ]
            if not audio_files:
                self.db.execute(
                    "UPDATE acquisitions SET status = 'failed', notes = ?, updated_at = ? WHERE id = ?",
                    ("No audio files found in the completed download", utcnow(), acquisition["id"]),
                )
                summary["failed"] += 1
                continue
            release_meta = json.loads(acquisition["metadata"] or "{}")
            organised_here = 0
            unmatched_here = 0
            file_results = []
            for path in audio_files:
                if not _is_safe_child(save_path, path):
                    summary["failed"] += 1
                    continue
                validation = validate_audio(path, ffprobe_path=self.config.audio.ffprobe_path)
                if not validation.ok:
                    file_results.append({"file": str(path), "status": "invalid", "errors": validation.errors})
                    continue
                tags = read_tags(path)
                match = self.matcher.identify(
                    tags,
                    duration_ms=validation.probe.duration_ms if validation.probe else None,
                    artist_hint=release_meta.get("creator"),
                    album_hint=acquisition["release_title"],
                )
                if match is None:
                    unmatched_here += 1
                    destination = self.config.verified_dir / f"unmatched-{acquisition['id']}" / path.name
                    if not dry_run:
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        if not destination.exists():
                            shutil.copy2(path, destination)
                    file_results.append({"file": str(path), "status": "unmatched", "copy": str(destination)})
                    continue
                track_id = match[0]
                if not dry_run:
                    self._tag_file(path, track_id, acquisition)
                    result = self.organiser.organise(path, track_id=track_id)
                    file_results.append({"file": str(path), **result.as_dict()})
                    if result.destination and result.action in {"move", "move_renamed", "already_present"}:
                        organised_here += 1
                else:
                    file_results.append({"file": str(path), "status": "would_organise", "track_id": track_id})
                    organised_here += 1
            summary["processed"] += 1
            summary["organised"] += organised_here
            summary["unmatched"] += unmatched_here
            summary["files"].extend(file_results)
            if not dry_run:
                status = "organised" if unmatched_here == 0 else "verified"
                self.db.execute(
                    "UPDATE acquisitions SET status = ?, updated_at = ? WHERE id = ?",
                    (status, utcnow(), acquisition["id"]),
                )
        return summary

    def _tag_file(self, path: Path, track_id: int, acquisition: Any) -> None:
        context = track_context(self.db, track_id)
        if context is None:
            return
        artwork = None
        cover_url = context.get("cover_url")
        if cover_url:
            artwork = fetch_image(cover_url)
        if artwork is None:
            album_row = self.db.one(
                "SELECT musicbrainz_release_group FROM albums WHERE id = ?", (context.get("album_id"),)
            )
            if album_row and album_row["musicbrainz_release_group"]:
                artwork = fetch_cover_art(release_group_mbid=album_row["musicbrainz_release_group"])
        if artwork:
            artwork, _ = prepare_artwork(
                artwork,
                target_px=self.config.device.artwork_size,
                ffmpeg_path=self.config.audio.ffmpeg_path,
            )
        metadata = TrackMetadata(
            title=context["title"],
            artist=context.get("artist_name") or "Unknown Artist",
            album=context.get("album_title") or "Unknown Album",
            album_artist=context.get("artist_name"),
            genre=", ".join(context.get("genres") or []) or None,
            track_number=context.get("track_number"),
            disc_number=context.get("disc_number") or 1,
            year=str(context.get("release_year")) if context.get("release_year") else None,
            label=context.get("label_name"),
            isrc=context.get("isrc"),
            source_url=context.get("source_url"),
            licence=acquisition["licence"],
        )
        notes = write_tags(path, metadata, artwork=artwork, preserve_existing=True)
        if notes:
            logger.info("Tag notes for %s: %s", path, notes)

    # ----------------------------------------------------------------- status
    def status(self) -> dict[str, Any]:
        jobs = [
            dict(row)
            for row in self.db.query(
                "SELECT j.*, a.status AS acquisition_status, r.title AS release_title, r.licence, "
                "r.authorisation FROM torrent_jobs j LEFT JOIN acquisitions a ON a.id = j.acquisition_id "
                "LEFT JOIN releases r ON r.id = a.release_id ORDER BY j.id DESC LIMIT 50"
            )
        ]
        available, version = self.qbit.is_available() if self.config.downloads.enabled else (
            False,
            "downloads disabled",
        )
        _ok, disk_message = (self.disk_check() if self.config.downloads.enabled else (True, "n/a"))
        return {
            "enabled": self.config.downloads.enabled,
            "qbit_available": available,
            "qbit_version": version if available else None,
            "disk": disk_message,
            "jobs": jobs,
            "counts": {
                "awaiting_approval": int(
                    self.db.scalar("SELECT COUNT(*) FROM acquisitions WHERE status = 'awaiting_approval'") or 0
                ),
                "approved": int(
                    self.db.scalar("SELECT COUNT(*) FROM acquisitions WHERE status = 'approved'") or 0
                ),
                "queued": int(self.db.scalar("SELECT COUNT(*) FROM acquisitions WHERE status = 'queued'") or 0),
                "downloaded": int(
                    self.db.scalar("SELECT COUNT(*) FROM acquisitions WHERE status = 'downloaded'") or 0
                ),
                "organised": int(
                    self.db.scalar("SELECT COUNT(*) FROM acquisitions WHERE status = 'organised'") or 0
                ),
            },
        }


# ---------------------------------------------------------------------- torrent hashing
def _bdecode_value(data: bytes, index: int) -> tuple[Any, int]:
    char = data[index : index + 1]
    if char == b"i":
        end = data.index(b"e", index)
        return int(data[index + 1 : end]), end + 1
    if char == b"l":
        index += 1
        items = []
        while data[index : index + 1] != b"e":
            value, index = _bdecode_value(data, index)
            items.append(value)
        return items, index + 1
    if char == b"d":
        index += 1
        result: dict[bytes, Any] = {}
        while data[index : index + 1] != b"e":
            key, index = _bdecode_value(data, index)
            value, index = _bdecode_value(data, index)
            result[key] = value
        return result, index + 1
    if char.isdigit():
        colon = data.index(b":", index)
        length = int(data[index:colon])
        start = colon + 1
        return data[start : start + length], start + length
    raise ValueError(f"Invalid bencode at offset {index}")


def infohash_from_torrent(data: bytes) -> str | None:
    marker = data.find(b"4:info")
    if marker == -1:
        return None
    start = marker + len(b"4:info")
    try:
        _, end = _bdecode_value(data, start)
    except (ValueError, IndexError):
        return None
    return hashlib.sha1(data[start:end]).hexdigest()


def infohash_from_magnet(url: str) -> str | None:
    from urllib.parse import parse_qs, urlparse

    query = parse_qs(urlparse(url).query)
    for xt in query.get("xt", []):
        if xt.startswith("urn:btih:"):
            return xt.split(":")[-1].lower()
    return None


def _is_safe_child(root: Path, candidate: Path) -> bool:
    try:
        root_resolved = root.resolve()
        candidate_resolved = candidate.resolve()
    except OSError:
        return False
    return root_resolved in candidate_resolved.parents or root_resolved == candidate_resolved
