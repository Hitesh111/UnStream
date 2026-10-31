"""Library scanning, file indexing and integrity checks."""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fiiolib.audio.validate import sha256_file, validate_audio
from fiiolib.config import Config
from fiiolib.db import Database, repo, utcnow
from fiiolib.metadata.matcher import TrackMatcher

logger = logging.getLogger(__name__)


@dataclass
class ScanSummary:
    scanned: int = 0
    indexed: int = 0
    matched: int = 0
    unmatched: int = 0
    invalid: int = 0
    changed: int = 0
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "scanned": self.scanned,
            "indexed": self.indexed,
            "matched": self.matched,
            "unmatched": self.unmatched,
            "invalid": self.invalid,
            "changed": self.changed,
            "errors": self.errors,
        }


class LibraryScanner:
    def __init__(self, config: Config, db: Database) -> None:
        self.config = config
        self.db = db
        self.matcher = TrackMatcher(db)

    def default_paths(self) -> list[Path]:
        return [self.config.music_dir, self.config.verified_dir]

    def scan(self, paths: Iterable[Path] | None = None, compute_hashes: bool = True) -> ScanSummary:
        summary = ScanSummary()
        extensions = {e.lower() for e in self.config.library.audio_extensions}
        roots = list(paths) if paths else self.default_paths()
        seen_paths: set[str] = set()
        for root in roots:
            if not root.exists():
                continue
            for path in sorted(root.rglob("*")):
                if not path.is_file() or path.suffix.lower() not in extensions:
                    continue
                if path.name.startswith("._") or path.name == ".DS_Store":
                    continue
                seen_paths.add(str(path))
                summary.scanned += 1
                try:
                    self._index_file(path, summary, compute_hashes)
                except Exception as exc:
                    summary.errors.append(f"{path}: {exc}")
                    logger.exception("Scanning failed for %s", path)

        for row in self.db.query("SELECT id, path FROM files"):
            if row["path"] not in seen_paths and not Path(row["path"]).exists():
                self.db.execute(
                    "UPDATE files SET validated = 0, validation_notes = ?, updated_at = ? WHERE id = ?",
                    ("File is missing from disk", utcnow(), row["id"]),
                )
        return summary

    def _index_file(self, path: Path, summary: ScanSummary, compute_hashes: bool) -> None:
        stat = path.stat()
        existing = self.db.one("SELECT * FROM files WHERE path = ?", (str(path),))
        unchanged = (
            existing is not None
            and existing["size_bytes"] == stat.st_size
            and abs(float(existing["mtime"] or 0) - stat.st_mtime) < 1.0
            and existing["sha256"]
        )
        if unchanged:
            if not existing["track_id"]:
                tags = _read_tags_quiet(path)
                match = self.matcher.identify(tags)
                if match:
                    self.db.execute(
                        "UPDATE files SET track_id = ?, updated_at = ? WHERE id = ?",
                        (match[0], utcnow(), existing["id"]),
                    )
                    summary.matched += 1
            return

        summary.changed += 1
        validation = validate_audio(
            path,
            ffprobe_path=self.config.audio.ffprobe_path,
            compute_hash=compute_hashes,
        )
        probe_info = validation.probe
        track_id = existing["track_id"] if existing else None
        if not track_id:
            tags = _read_tags_quiet(path)
            match = self.matcher.identify(tags, duration_ms=probe_info.duration_ms if probe_info else None)
            if match:
                track_id = match[0]
                summary.matched += 1
            else:
                summary.unmatched += 1
        file_id = repo.upsert_file(
            self.db,
            str(path),
            track_id=track_id,
            size_bytes=stat.st_size,
            mtime=stat.st_mtime,
            sha256=validation.sha256 or (existing["sha256"] if existing else None),
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
        )
        self.db.execute(
            "INSERT INTO audio_validations(file_id, ok, findings, probe) VALUES (?, ?, ?, ?)",
            (
                file_id,
                1 if validation.ok else 0,
                json.dumps(validation.findings + validation.errors + validation.warnings),
                json.dumps(validation.probe.as_dict() if validation.probe else {}),
            ),
        )
        summary.indexed += 1
        if not validation.ok:
            summary.invalid += 1

    def integrity_check(self, deep: bool = False) -> list[dict[str, Any]]:
        issues: list[dict[str, Any]] = []
        for row in self.db.query("SELECT * FROM files"):
            path = Path(row["path"])
            if not path.exists():
                issues.append({"file_id": row["id"], "path": str(path), "issue": "missing"})
                continue
            stat = path.stat()
            if row["size_bytes"] and stat.st_size != row["size_bytes"]:
                issues.append(
                    {
                        "file_id": row["id"],
                        "path": str(path),
                        "issue": "size_changed",
                        "expected": row["size_bytes"],
                        "actual": stat.st_size,
                    }
                )
            if deep and row["sha256"]:
                actual = sha256_file(path)
                if actual != row["sha256"]:
                    issues.append(
                        {"file_id": row["id"], "path": str(path), "issue": "checksum_mismatch"}
                    )
            if row["validated"] == 0 and row["validation_notes"]:
                issues.append(
                    {
                        "file_id": row["id"],
                        "path": str(path),
                        "issue": "validation_failed",
                        "detail": row["validation_notes"],
                    }
                )
        return issues

    def orphan_tracks(self) -> list[dict[str, Any]]:
        rows = self.db.query(
            "SELECT t.id, t.title, ar.name AS artist FROM tracks t "
            "LEFT JOIN artists ar ON ar.id = t.artist_id "
            "LEFT JOIN files f ON f.track_id = t.id WHERE f.id IS NULL"
        )
        return [dict(row) for row in rows]


def _read_tags_quiet(path: Path) -> dict[str, Any]:
    try:
        from fiiolib.metadata.tagger import read_tags

        return read_tags(path)
    except Exception:
        return {}
