"""Duplicate detection (never deletes files automatically)."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from fiiolib.config import Config
from fiiolib.db import Database

logger = logging.getLogger(__name__)


@dataclass
class DuplicateGroup:
    kind: str
    key: str
    files: list[dict[str, Any]] = field(default_factory=list)
    keeper: int | None = None
    reason: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "key": self.key,
            "keeper": self.keeper,
            "reason": self.reason,
            "files": self.files,
        }


class Deduplicator:
    def __init__(self, config: Config, db: Database) -> None:
        self.config = config
        self.db = db

    def find(self) -> list[DuplicateGroup]:
        groups: list[DuplicateGroup] = []
        for row in self.db.query(
            "SELECT sha256 FROM files WHERE sha256 IS NOT NULL GROUP BY sha256 HAVING COUNT(*) > 1"
        ):
            files = [
                dict(item)
                for item in self.db.query(
                    "SELECT id, path, size_bytes, codec, bit_depth, sample_rate, validated, organised_at "
                    "FROM files WHERE sha256 = ?",
                    (row["sha256"],),
                )
            ]
            group = DuplicateGroup(kind="checksum", key=row["sha256"], files=files)
            self._choose_keeper(group)
            groups.append(group)

        if self.config.library.duplicate_detection:
            for row in self.db.query(
                "SELECT t.id AS track_id, COUNT(f.id) AS n FROM files f JOIN tracks t ON t.id = f.track_id "
                "GROUP BY t.id HAVING COUNT(f.id) > 1"
            ):
                files = [
                    dict(item)
                    for item in self.db.query(
                        "SELECT f.id, f.path, f.size_bytes, f.codec, f.bit_depth, f.sample_rate, "
                        "f.validated, f.organised_at FROM files f WHERE f.track_id = ?",
                        (row["track_id"],),
                    )
                ]
                checksum_file_ids = {item["id"] for group in groups for item in group.files}
                if {item["id"] for item in files} <= checksum_file_ids:
                    continue
                group = DuplicateGroup(
                    kind="track", key=f"track:{row['track_id']}", files=files
                )
                self._choose_keeper(group)
                groups.append(group)
        return groups

    def _choose_keeper(self, group: DuplicateGroup) -> None:
        def score(item: dict[str, Any]) -> tuple:
            path = str(item["path"])
            inside_music = str(self.config.music_dir) in path
            return (
                1 if item.get("organised_at") else 0,
                1 if inside_music else 0,
                1 if item.get("validated") else 0,
                1 if (item.get("codec") or "").lower() == "flac" else 0,
                int(item.get("bit_depth") or 0),
                int(item.get("sample_rate") or 0),
                -int(item.get("size_bytes") or 0),
                -item["id"],
            )

        ranked = sorted(group.files, key=score, reverse=True)
        if ranked:
            group.keeper = int(ranked[0]["id"])
            group.reason = "Prefer organised, validated, lossless, highest-resolution copy"

    def record(self, groups: list[DuplicateGroup]) -> int:
        recorded = 0
        for group in groups:
            for item in group.files:
                action = "keep" if item["id"] == group.keeper else "candidate_duplicate"
                self.db.execute(
                    "INSERT INTO dedupe_actions(file_id, duplicate_of, action) SELECT ?, ?, ? "
                    "WHERE NOT EXISTS (SELECT 1 FROM dedupe_actions WHERE file_id = ? AND action = ?)",
                    (item["id"], group.keeper, action, item["id"], action),
                )
                recorded += 1
        return recorded

    def summary(self) -> dict[str, Any]:
        groups = self.find()
        self.record(groups)
        reclaimable = 0
        for group in groups:
            for item in group.files:
                if item["id"] != group.keeper:
                    reclaimable += int(item.get("size_bytes") or 0)
        return {
            "groups": len(groups),
            "duplicate_files": sum(max(0, len(g.files) - 1) for g in groups),
            "reclaimable_bytes": reclaimable,
            "reclaimable_gb": round(reclaimable / 1024**3, 3),
            "groups_detail": [g.as_dict() for g in groups],
            "note": "No files are deleted automatically. Review candidates before removing anything.",
        }
