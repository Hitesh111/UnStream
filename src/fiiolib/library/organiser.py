"""Deterministic, device-safe organisation of verified audio into the library."""

from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fiiolib.audio.device_profile import device_filename, sanitize_component
from fiiolib.audio.validate import sha256_file, validate_audio
from fiiolib.config import Config
from fiiolib.db import Database, repo, utcnow
from fiiolib.library.queries import track_context
from fiiolib.metadata.classification import classify_genres
from fiiolib.metadata.matcher import TrackMatcher
from fiiolib.metadata.tagger import read_tags

logger = logging.getLogger(__name__)


@dataclass
class OrganiseResult:
    source: str
    destination: str | None = None
    track_id: int | None = None
    action: str = "skipped"
    notes: list[str] = field(default_factory=list)
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "destination": self.destination,
            "track_id": self.track_id,
            "action": self.action,
            "notes": self.notes,
            "error": self.error,
        }


class Organiser:
    def __init__(self, config: Config, db: Database) -> None:
        self.config = config
        self.db = db
        self.matcher = TrackMatcher(db)

    # ------------------------------------------------------------- planning
    def plan(self, path: Path, track_id: int | None = None) -> OrganiseResult:
        tags = read_tags(path)
        if track_id is None:
            match = self.matcher.identify(tags)
            if match:
                track_id = match[0]
        if track_id is None:
            return OrganiseResult(
                source=str(path),
                action="unmatched",
                notes=["No database track matched this file"],
            )
        context = track_context(self.db, track_id)
        if context is None:
            return OrganiseResult(source=str(path), action="unmatched", track_id=track_id,
                                  notes=["Track row missing from database"])

        classification = classify_genres(context["genres"])
        artist = sanitize_component(context.get("artist_name") or "Unknown Artist")
        album = sanitize_component(context.get("album_title") or "Unknown Album")
        title = context.get("title") or path.stem
        filename = device_filename(context.get("track_number"), title, path.suffix)
        relative = Path(classification.category) / classification.subgenre / artist / album
        disc = context.get("disc_number") or 1
        if disc and int(disc) > 1:
            relative = relative / f"Disc {int(disc)}"
        destination = self.config.music_dir / relative / filename
        result = OrganiseResult(
            source=str(path), destination=str(destination), track_id=track_id, action="move"
        )
        result.notes.append(f"Classified as {classification.relative_dir}")
        return result

    # ------------------------------------------------------------ execution
    def organise(
        self,
        path: str | Path,
        track_id: int | None = None,
        copy: bool = False,
        dry_run: bool = False,
        overwrite: bool = False,
    ) -> OrganiseResult:
        source = Path(path)
        if not source.exists():
            return OrganiseResult(source=str(source), action="error", error="Source file missing")

        result = self.plan(source, track_id)
        if result.destination is None:
            return result
        destination = Path(result.destination)
        previous = self.db.one("SELECT id FROM files WHERE path = ?", (str(source),))

        def forget_source() -> None:
            if not copy and previous and str(source) != str(destination):
                self.db.execute(
                    "DELETE FROM files WHERE id = ? AND path = ?", (previous["id"], str(source))
                )

        if destination.exists() and not overwrite:
            existing_hash = sha256_file(destination)
            source_hash = sha256_file(source)
            if existing_hash == source_hash:
                result.action = "already_present"
                result.notes.append("Identical file already organised")
                if not copy:
                    source.unlink(missing_ok=True)
                self._record_file(destination, result.track_id, already_organised=True)
                forget_source()
                return result
            destination = _unique_path(destination)
            result.destination = str(destination)
            result.action = "move_renamed"
            result.notes.append("Destination existed with different audio; used a suffixed name")

        if dry_run:
            result.action = "would_" + result.action
            return result

        destination.parent.mkdir(parents=True, exist_ok=True)
        if copy:
            shutil.copy2(source, destination)
        else:
            shutil.move(str(source), str(destination))

        file_id = self._record_file(destination, result.track_id, already_organised=True)
        forget_source()
        self.db.execute(
            "UPDATE files SET organised_at = ?, updated_at = ? WHERE id = ?",
            (utcnow(), utcnow(), file_id),
        )
        self.db.record_log("INFO", "organiser", "File organised", result.as_dict())
        return result

    def _record_file(self, path: Path, track_id: int | None, already_organised: bool = False) -> int:
        validation = validate_audio(path, ffprobe_path=self.config.audio.ffprobe_path, compute_hash=True)
        probe_info = validation.probe
        file_id = repo.upsert_file(
            self.db,
            str(path),
            track_id=track_id,
            size_bytes=path.stat().st_size,
            mtime=path.stat().st_mtime,
            sha256=validation.sha256,
            format=probe_info.format_name if probe_info else None,
            codec=probe_info.codec if probe_info else None,
            bit_depth=probe_info.bit_depth if probe_info else None,
            sample_rate=probe_info.sample_rate if probe_info else None,
            channels=probe_info.channels if probe_info else None,
            bitrate=probe_info.bitrate if probe_info else None,
            duration_ms=probe_info.duration_ms if probe_info else None,
            validated=1 if validation.ok else 0,
            validation_notes="; ".join(validation.errors + validation.warnings) or None,
            transcode_suspect=1 if validation.transcode_suspect else 0,
            organised_at=utcnow() if already_organised else None,
        )
        import json

        self.db.execute(
            "INSERT INTO audio_validations(file_id, ok, findings, probe) VALUES (?, ?, ?, ?)",
            (
                file_id,
                1 if validation.ok else 0,
                json.dumps(validation.findings + validation.errors + validation.warnings),
                json.dumps(validation.probe.as_dict() if validation.probe else {}),
            ),
        )
        return file_id

    def organise_directory(self, directory: Path, copy: bool = False, dry_run: bool = False) -> list[OrganiseResult]:
        results: list[OrganiseResult] = []
        extensions = {e.lower() for e in self.config.library.audio_extensions}
        for path in sorted(directory.rglob("*")):
            if not path.is_file() or path.suffix.lower() not in extensions:
                continue
            file_row = self.db.one("SELECT track_id FROM files WHERE path = ?", (str(path),))
            track_id = int(file_row["track_id"]) if file_row and file_row["track_id"] else None
            results.append(self.organise(path, track_id=track_id, copy=copy, dry_run=dry_run))
        return results


def _unique_path(path: Path) -> Path:
    counter = 2
    while True:
        candidate = path.with_name(f"{path.stem} ({counter}){path.suffix}")
        if not candidate.exists():
            return candidate
        counter += 1
