"""Dry-run-first, checksummed, incremental synchronisation to the microSD card."""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fiiolib.activity import activity
from fiiolib.audio.device_profile import sanitize_component
from fiiolib.audio.validate import sha256_file
from fiiolib.config import Config
from fiiolib.db import Database, utcnow
from fiiolib.device.detect import (
    DeviceError,
    DeviceLocator,
    VolumeInfo,
    device_identifier,
)
from fiiolib.library.queries import track_context
from fiiolib.metadata.classification import classify_genres
from fiiolib.sync.selection import select_files

logger = logging.getLogger(__name__)

COPY_CHUNK = 1024 * 1024


@dataclass
class SyncItem:
    file_id: int
    source: str
    destination: str
    relative: str
    size: int
    sha256: str | None
    action: str
    reason: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "file_id": self.file_id,
            "source": self.source,
            "destination": self.destination,
            "relative": self.relative,
            "size": self.size,
            "sha256": self.sha256,
            "action": self.action,
            "reason": self.reason,
        }


@dataclass
class SyncPlan:
    device: VolumeInfo
    device_id: str
    root: Path
    items: list[SyncItem] = field(default_factory=list)
    unmanaged: list[dict[str, Any]] = field(default_factory=list)
    free_bytes: int = 0
    reserve_bytes: int = 0
    planned_bytes: int = 0
    fits: bool = True
    warnings: list[str] = field(default_factory=list)
    profile: str = "complete"

    @property
    def counts(self) -> dict[str, int]:
        counts = {"add": 0, "update": 0, "skip": 0, "no_space": 0}
        for item in self.items:
            counts[item.action] = counts.get(item.action, 0) + 1
        return counts

    def as_dict(self) -> dict[str, Any]:
        return {
            "device": self.device.as_dict(),
            "device_id": self.device_id,
            "root": str(self.root),
            "profile": self.profile,
            "counts": self.counts,
            "files": len(self.items),
            "planned_bytes": self.planned_bytes,
            "planned_gb": round(self.planned_bytes / 1024**3, 3),
            "free_bytes": self.free_bytes,
            "free_gb": round(self.free_bytes / 1024**3, 3),
            "reserve_gb": round(self.reserve_bytes / 1024**3, 3),
            "fits": self.fits,
            "unmanaged": self.unmanaged,
            "warnings": self.warnings,
            "items": [item.as_dict() for item in self.items],
        }


class SyncPlanner:
    def __init__(self, config: Config, db: Database) -> None:
        self.config = config
        self.db = db
        self.locator = DeviceLocator(config)

    # ------------------------------------------------------------- device
    def register(self, volume: VolumeInfo) -> int:
        identifier = device_identifier(volume)
        row = self.db.one("SELECT id FROM devices WHERE identifier = ?", (identifier,))
        payload = (
            volume.volume_name,
            volume.mount_point,
            volume.filesystem,
            volume.capacity_bytes,
            utcnow(),
        )
        if row:
            self.db.execute(
                "UPDATE devices SET volume_name = ?, mount_point = ?, filesystem = ?, "
                "capacity_bytes = ?, last_seen_at = ? WHERE id = ?",
                (*payload, row["id"]),
            )
            return int(row["id"])
        cursor = self.db.execute(
            "INSERT INTO devices(identifier, volume_name, mount_point, filesystem, capacity_bytes, "
            "is_echo_mini, confirmed, last_seen_at) VALUES (?, ?, ?, ?, ?, ?, 0, ?)",
            (identifier, volume.volume_name, volume.mount_point, volume.filesystem,
             volume.capacity_bytes, 1 if volume.is_echo_candidate else 0, utcnow()),
        )
        return int(cursor.lastrowid)

    def confirm(self, volume: VolumeInfo) -> int:
        device_db_id = self.register(volume)
        self.db.execute(
            "UPDATE devices SET confirmed = 1, notes = COALESCE(notes, 'Confirmed by user') WHERE id = ?",
            (device_db_id,),
        )
        return device_db_id

    def is_confirmed(self, volume: VolumeInfo) -> bool:
        row = self.db.one(
            "SELECT confirmed FROM devices WHERE identifier = ?", (device_identifier(volume),)
        )
        return bool(row and row["confirmed"])

    # --------------------------------------------------------------- plan
    def plan(
        self,
        device_identifier_hint: str | None = None,
        profile: str = "complete",
        genres: list[str] | None = None,
        artist_ids: list[int] | None = None,
        album_ids: list[int] | None = None,
        fast: bool = False,
    ) -> SyncPlan:
        volume = self.locator.current(device_identifier_hint)
        if not volume.mount_point:
            raise DeviceError("The selected volume is not mounted")
        if not self.is_confirmed(volume):
            raise DeviceError(
                "This card has not been confirmed yet. Run "
                f"`musicctl device inspect --device {volume.volume_name!r}` and then "
                f"`musicctl device confirm --device {volume.volume_name!r}` before syncing."
            )
        root = Path(volume.mount_point) / self.config.device.music_subdir
        usage = shutil.disk_usage(Path(volume.mount_point))
        reserve = int(self.config.device.reserve_free_gb * 1024**3)
        available = max(0, usage.free - reserve)

        selection = select_files(
            self.db,
            profile=profile,
            genres=genres,
            artist_ids=artist_ids,
            album_ids=album_ids,
            available_bytes=available if profile == "offline" else None,
        )
        plan = SyncPlan(
            device=volume,
            device_id=device_identifier(volume),
            root=root,
            free_bytes=usage.free,
            reserve_bytes=reserve,
            profile=profile,
        )
        plan.warnings.extend(selection.notes)
        planned: dict[str, SyncItem] = {}
        for file_row in selection.files:
            relative = self._relative_path(file_row)
            if relative is None:
                plan.warnings.append(f"Could not derive a destination for {file_row['path']}")
                continue
            destination = root / relative
            size = int(file_row.get("size_bytes") or 0)
            source_hash = file_row.get("sha256")
            action = "add"
            reason = None
            if destination.exists():
                dest_size = destination.stat().st_size
                if dest_size == size:
                    if fast or not source_hash:
                        action = "skip"
                        reason = "Same size (fast mode)"
                    else:
                        if sha256_file(destination) == source_hash:
                            action = "skip"
                            reason = "Checksum matches"
                        else:
                            action = "update"
                            reason = "Checksum differs"
                else:
                    action = "update"
                    reason = f"Size differs ({dest_size} vs {size})"
            if action in {"add", "update"}:
                if size > available:
                    action = "no_space"
                    reason = "Does not fit in reserved free space"
                else:
                    available -= size
                    plan.planned_bytes += size
            item = SyncItem(
                file_id=int(file_row["id"]),
                source=str(file_row["path"]),
                destination=str(destination),
                relative=str(relative),
                size=size,
                sha256=source_hash,
                action=action,
                reason=reason,
            )
            planned[str(destination)] = item
        plan.items = list(planned.values())
        plan.fits = plan.planned_bytes <= (usage.free - reserve)
        plan.unmanaged = [] if not root.exists() else self._unmanaged(root, planned)
        return plan

    def _relative_path(self, file_row: dict[str, Any]) -> Path | None:
        source = Path(file_row["path"])
        try:
            relative = source.relative_to(self.config.music_dir)
            if relative.parts and relative.parts[0] in {"."}:
                return None
            return relative
        except ValueError:
            pass
        context = track_context(self.db, int(file_row["track_id"]))
        if context is None:
            return None
        classification = classify_genres(context.get("genres") or [])
        artist = sanitize_component(context.get("artist_name") or "Unknown Artist")
        album = sanitize_component(context.get("album_title") or "Unknown Album")
        relative = Path(classification.category) / classification.subgenre / artist / album
        disc = context.get("disc_number") or 1
        if int(disc) > 1:
            relative = relative / f"Disc {int(disc)}"
        return relative / source.name

    def _unmanaged(self, root: Path, planned: dict[str, SyncItem]) -> list[dict[str, Any]]:
        extensions = {e.lower() for e in self.config.library.audio_extensions}
        results: list[dict[str, Any]] = []
        planned_paths = set(planned)
        for path in root.rglob("*"):
            if not path.is_file() or path.suffix.lower() not in extensions:
                continue
            if path.name.startswith("._"):
                continue
            if str(path) not in planned_paths:
                results.append(
                    {
                        "path": str(path),
                        "size": path.stat().st_size,
                        "note": "Present on card but not in this selection; left untouched",
                    }
                )
        return results

    # ------------------------------------------------------------ execute
    def execute(self, plan: SyncPlan, dry_run: bool = True, verify: bool = True) -> dict[str, Any]:
        if not self.is_confirmed(plan.device):
            raise DeviceError("Refusing to write to an unconfirmed device")
        run_id = int(
            self.db.execute(
                "INSERT INTO sync_runs(device_id, dry_run, status, planned_bytes) VALUES (?, ?, ?, ?)",
                (self.register(plan.device), 1 if dry_run else 0, "planned" if dry_run else "running", plan.planned_bytes),
            ).lastrowid
        )
        total_write_items = len([i for i in plan.items if i.action in {"add", "update"}])
        if not dry_run:
            activity.start_operation(
                action="sync",
                description=f"Syncing {total_write_items} tracks to {plan.device.volume_name}",
                total=total_write_items,
            )
            activity.log(
                level="info",
                category="sync",
                message=f"Starting microSD card sync to {plan.device.volume_name} ({total_write_items} to copy, {len(plan.items)} total planned)",
            )
        else:
            activity.log(
                level="info",
                category="sync",
                message=f"Sync preview computed: {len(plan.items)} files ({total_write_items} to write, {plan.planned_bytes // (1024*1024)} MB)",
            )

        result = {
            "run_id": run_id,
            "dry_run": dry_run,
            "added": 0,
            "updated": 0,
            "skipped": 0,
            "verified": 0,
            "errors": [],
            "plan": plan.as_dict(),
        }
        for item in plan.items:
            if item.action not in {"add", "update"}:
                if item.action == "skip":
                    result["skipped"] += 1
                self._record_item(run_id, item, "skipped" if item.action in {"skip", "no_space"} else item.action)
                continue
            if dry_run:
                self._record_item(run_id, item, "planned")
                continue

            dest_name = Path(item.relative).name
            activity.update_progress(
                current=result["added"] + result["updated"] + 1,
                total=total_write_items,
                current_item=item.relative,
            )
            activity.log(
                level="info",
                category="sync",
                message=f"Copying '{dest_name}' ({item.size // 1024} KB) to microSD…",
                track_title=Path(item.relative).stem,
            )
            try:
                self._copy_verified(item, verify=verify)
                self._record_item(run_id, item, "verified" if verify else "copied")
                if item.action == "add":
                    result["added"] += 1
                else:
                    result["updated"] += 1
                result["verified"] += 1 if verify else 0
                activity.log(
                    level="success",
                    category="sync",
                    message=f"✓ Verified on card: '{dest_name}' (SHA-256 match)",
                    track_title=Path(item.relative).stem,
                )
            except Exception as exc:
                logger.exception("Sync failed for %s", item.source)
                result["errors"].append(f"{item.relative}: {exc}")
                self._record_item(run_id, item, "failed", error=str(exc))
                activity.log(
                    level="error",
                    category="sync",
                    message=f"✗ Card write failed for '{dest_name}': {exc}",
                    track_title=Path(item.relative).stem,
                    error=str(exc),
                )

        if not dry_run and not result["errors"]:
            result["albums_synced"] = self._mark_albums_synced(plan)
            # macOS automatically creates AppleDouble ._* files on FAT/exFAT volumes,
            # which confuse the Echo Mini display. Clean them automatically after sync.
            if sys.platform == "darwin" and shutil.which("dot_clean"):
                activity.log(
                    level="info",
                    category="sync",
                    message="Running macOS dot_clean to merge AppleDouble resource fork files…",
                )
                try:
                    music_dir = plan.device.mount_point / "Music"
                    if music_dir.exists():
                        subprocess.run(
                            ["dot_clean", "-m", str(music_dir)],
                            capture_output=True,
                            check=False,
                            timeout=30,
                        )
                except Exception:
                    pass

        if not dry_run:
            activity.finish_operation(
                summary=f"Card sync complete: {result['added']} added, {len(result['errors'])} failed"
            )
            activity.log(
                level="success" if not result["errors"] else "warn",
                category="sync",
                message=f"Card sync finished: {result['added']} files written, {result['skipped']} skipped, {len(result['errors'])} errors",
            )
        self.db.execute(
            "UPDATE sync_runs SET finished_at = ?, status = ?, added = ?, updated = ?, skipped = ?, "
            "copied_bytes = ?, errors = ? WHERE id = ?",
            (
                utcnow(),
                "dry_run" if dry_run else ("completed" if not result["errors"] else "completed_with_errors"),
                result["added"],
                result["updated"],
                result["skipped"],
                plan.planned_bytes if not dry_run else 0,
                "\n".join(result["errors"]) or None,
                run_id,
            ),
        )
        return result

    def _mark_albums_synced(self, plan: SyncPlan) -> int:
        album_ids: set[int] = set()
        file_ids = [item.file_id for item in plan.items]
        for start in range(0, len(file_ids), 400):
            chunk = file_ids[start : start + 400]
            placeholders = ",".join(["?"] * len(chunk))
            rows = self.db.query(
                "SELECT DISTINCT t.album_id FROM files f JOIN tracks t ON t.id = f.track_id "
                f"WHERE f.id IN ({placeholders}) AND t.album_id IS NOT NULL",
                chunk,
            )
            album_ids.update(int(row["album_id"]) for row in rows)
        marked = 0
        for album_id in album_ids:
            files = [
                dict(row)
                for row in self.db.query(
                    "SELECT f.id, f.path, f.track_id FROM files f JOIN tracks t ON t.id = f.track_id "
                    "WHERE t.album_id = ? AND f.organised_at IS NOT NULL AND f.validated = 1",
                    (album_id,),
                )
            ]
            if not files:
                continue
            complete = True
            for file_row in files:
                relative = self._relative_path(file_row)
                if relative is None or not (plan.root / relative).exists():
                    complete = False
                    break
            if complete:
                cursor = self.db.execute(
                    "UPDATE discovery_candidates SET status = 'synced', updated_at = ? "
                    "WHERE entity_type = 'album' AND album_id = ? "
                    "AND status NOT IN ('rejected', 'failed', 'synced')",
                    (utcnow(), album_id),
                )
                marked += cursor.rowcount
        return marked

    def _copy_verified(self, item: SyncItem, verify: bool) -> None:
        source = Path(item.source)
        destination = Path(item.destination)
        if not source.exists():
            raise FileNotFoundError(item.source)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.part")
        try:
            with source.open("rb") as src, temporary.open("wb") as dst:
                shutil.copyfileobj(src, dst, COPY_CHUNK)
                dst.flush()
                os.fsync(dst.fileno())
            shutil.copystat(source, temporary)
            if verify:
                source_hash = item.sha256 or sha256_file(source)
                copied_hash = sha256_file(temporary)
                if copied_hash != source_hash:
                    raise OSError("Checksum mismatch after copy")
            os.replace(temporary, destination)
        finally:
            if temporary.exists():
                temporary.unlink(missing_ok=True)

    def _record_item(self, run_id: int, item: SyncItem, status: str, error: str | None = None) -> None:
        self.db.execute(
            "INSERT INTO sync_items(sync_run_id, file_id, action, src_path, dest_path, bytes, checksum, status, error) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                run_id,
                item.file_id,
                item.action,
                item.source,
                item.destination,
                item.size,
                item.sha256,
                status,
                error,
            ),
        )
