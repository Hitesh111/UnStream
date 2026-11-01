"""Local folder scanner and importer: scans arbitrary audio collections,
extracts or infers tags, validates device compatibility, and organises into library.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fiiolib.activity import activity
from fiiolib.audio.device_profile import sanitize_component
from fiiolib.audio.probe import probe
from fiiolib.config import Config
from fiiolib.db import Database, repo
from fiiolib.library.organiser import Organiser
from fiiolib.metadata.tagger import read_tags

logger = logging.getLogger(__name__)

SUPPORTED_EXTENSIONS = {
    ".flac",
    ".mp3",
    ".m4a",
    ".aac",
    ".ogg",
    ".wav",
    ".ape",
    ".dsf",
    ".dff",
    ".wma",
}


@dataclass
class ScanSummary:
    folder_path: str
    total_files: int
    total_bytes: int
    extensions: dict[str, int]
    sample_tracks: list[dict[str, Any]]

    def as_dict(self) -> dict[str, Any]:
        return {
            "folder_path": self.folder_path,
            "total_files": self.total_files,
            "total_bytes": self.total_bytes,
            "total_mb": round(self.total_bytes / (1024 * 1024), 1),
            "total_gb": round(self.total_bytes / (1024**3), 2),
            "extensions": self.extensions,
            "sample_tracks": self.sample_tracks,
        }


def scan_folder(folder_path: Path | str, max_samples: int = 10) -> ScanSummary:
    """Preview audio tracks in a folder without modifying any files."""
    path = Path(folder_path).expanduser().resolve()
    if not path.is_dir():
        return ScanSummary(str(path), 0, 0, {}, [])

    total_files = 0
    total_bytes = 0
    extensions: dict[str, int] = {}
    samples: list[dict[str, Any]] = []

    for item in sorted(path.rglob("*")):
        if item.is_file() and item.suffix.lower() in SUPPORTED_EXTENSIONS:
            ext = item.suffix.lower()
            extensions[ext] = extensions.get(ext, 0) + 1
            size = item.stat().st_size
            total_files += 1
            total_bytes += size

            if len(samples) < max_samples:
                tags = read_tags(item)
                title = str(tags.get("title") or item.stem)
                artist = str(tags.get("artist") or item.parent.parent.name or "Unknown")
                album = str(tags.get("album") or item.parent.name or "Unknown")
                samples.append(
                    {
                        "path": str(item),
                        "filename": item.name,
                        "title": title,
                        "artist": artist,
                        "album": album,
                        "format": ext.lstrip("."),
                        "size_bytes": size,
                    }
                )

    return ScanSummary(
        folder_path=str(path),
        total_files=total_files,
        total_bytes=total_bytes,
        extensions=extensions,
        sample_tracks=samples,
    )


class FolderImporter:
    def __init__(self, config: Config, db: Database) -> None:
        self.config = config
        self.db = db
        self.organiser = Organiser(config, db)

    def import_folder(
        self,
        folder_path: Path | str,
        copy_files: bool = True,
        convert_unsupported: bool = True,
    ) -> dict[str, Any]:
        """Import all audio files from folder_path into the verified library."""
        src_path = Path(folder_path).expanduser().resolve()
        if not src_path.is_dir():
            return {"ok": False, "error": f"Folder {src_path} does not exist or is not a directory"}

        activity.log(
            level="info",
            category="import",
            message=f"Scanning folder for audio tracks: {src_path}",
        )

        audio_files: list[Path] = []
        for p in sorted(src_path.rglob("*")):
            if p.is_file() and p.suffix.lower() in SUPPORTED_EXTENSIONS:
                audio_files.append(p)

        if not audio_files:
            activity.log(
                level="warn",
                category="import",
                message=f"No supported audio tracks found in {src_path}",
            )
            return {"ok": True, "total_found": 0, "imported": 0, "skipped": 0, "errors": []}

        activity.log(
            level="info",
            category="import",
            message=f"Found {len(audio_files)} tracks to import. Starting processing…",
        )

        imported = 0
        skipped = 0
        errors: list[str] = []

        for idx, file_path in enumerate(audio_files, 1):
            try:
                # Check if file is already indexed
                existing = self.db.one("SELECT id, track_id FROM files WHERE path = ?", (str(file_path),))
                if existing and existing["id"]:
                    skipped += 1
                    continue

                # Read tags
                tags = read_tags(file_path)
                title = str(tags.get("title") or "").strip()
                artist = str(tags.get("artist") or "").strip()
                album = str(tags.get("album") or "").strip()
                track_num = tags.get("tracknumber")
                disc_num = tags.get("discnumber") or 1
                year = str(tags.get("date") or tags.get("year") or "")[:4]
                genre = str(tags.get("genre") or "Unsorted")

                # Parse filename fallback if tags are blank: e.g. "01 - Artist - Title.mp3"
                if not title or not artist:
                    stem = file_path.stem
                    parts = [p.strip() for p in stem.split(" - ") if p.strip()]
                    if len(parts) >= 2:
                        if re.match(r"^\d+$", parts[0]):
                            track_num = parts[0]
                            if len(parts) == 2:
                                title = parts[1]
                            elif len(parts) >= 3:
                                artist = parts[1]
                                title = parts[2]
                        else:
                            artist = parts[0]
                            title = parts[1]
                    else:
                        title = stem

                if not artist:
                    artist = file_path.parent.parent.name if file_path.parent.parent != src_path else "Unknown Artist"
                if not album:
                    album = file_path.parent.name if file_path.parent != src_path else "Unknown Album"

                artist = sanitize_component(artist or "Unknown Artist")
                album = sanitize_component(album or "Unknown Album")
                title = title or file_path.stem

                track_number = None
                if track_num:
                    with_clean = str(track_num).split("/")[0].strip()
                    if with_clean.isdigit():
                        track_number = int(with_clean)

                # Upsert into DB
                artist_id = repo.get_or_create_artist(self.db, artist)
                album_id = repo.get_or_create_album(
                    self.db,
                    album,
                    artist_id=artist_id,
                    release_year=int(year) if year.isdigit() else None,
                )
                track_id = repo.get_or_create_track(
                    self.db,
                    title,
                    artist_id=artist_id,
                    album_id=album_id,
                    track_number=track_number,
                    disc_number=int(disc_num) if str(disc_num).isdigit() else 1,
                    duration_ms=tags.get("_duration_ms"),
                )

                # Check if transcoding needed for DAP compatibility
                pr = probe(file_path, self.config.audio.ffprobe_path)
                source_to_organise = file_path

                # If FLAC has non-standard block size or bit depth, normalize to standard 4096 16-bit FLAC
                needs_normalize = False
                if pr.codec == "flac":
                    if pr.flac_max_blocksize and pr.flac_max_blocksize != 4096:
                        needs_normalize = True
                    if pr.bit_depth and pr.bit_depth > 16:
                        needs_normalize = True

                if convert_unsupported and needs_normalize:
                    staging_tmp = self.config.root / "staging" / f"import_{track_id}.flac"
                    staging_tmp.parent.mkdir(parents=True, exist_ok=True)
                    cmd = [
                        "ffmpeg",
                        "-y",
                        "-i",
                        str(file_path),
                        "-map",
                        "0",
                        "-c:a",
                        "flac",
                        "-af",
                        "asetnsamples=n=4096:p=0",
                        "-sample_fmt",
                        "s16",
                        "-c:v",
                        "copy",
                        str(staging_tmp),
                    ]
                    c_res = subprocess.run(cmd, capture_output=True, text=True)
                    if c_res.returncode == 0 and staging_tmp.exists():
                        source_to_organise = staging_tmp

                org_res = self.organiser.organise(
                    source_to_organise,
                    track_id=track_id,
                    copy=copy_files or (source_to_organise != file_path),
                )

                if org_res.action in ("move", "copied", "already_present"):
                    imported += 1
                    activity.log(
                        level="info",
                        category="import",
                        message=f"[{idx}/{len(audio_files)}] ✓ Imported: '{title}' by {artist}",
                        track_id=track_id,
                        track_title=title,
                        artist=artist,
                    )
                else:
                    skipped += 1
            except Exception as exc:
                err_msg = f"Failed to import {file_path.name}: {exc}"
                logger.exception("Import error on %s", file_path)
                errors.append(err_msg)
                activity.log(
                    level="error",
                    category="import",
                    message=f"✗ {err_msg[:120]}",
                )

        activity.log(
            level="success",
            category="import",
            message=f"Import complete: {imported} imported, {skipped} skipped, {len(errors)} failed",
        )

        return {
            "ok": True,
            "total_found": len(audio_files),
            "imported": imported,
            "skipped": skipped,
            "errors": errors,
        }
