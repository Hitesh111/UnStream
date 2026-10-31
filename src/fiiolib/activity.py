"""Live activity logger and execution monitor for dashboard."""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any


@dataclass
class ActivityEntry:
    id: int
    timestamp: str  # HH:MM:SS
    iso_time: str
    level: str  # info, success, warn, error
    category: str  # sync, download, transcode, validate, system
    message: str
    track_id: int | None = None
    track_title: str | None = None
    artist: str | None = None
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class ActivityLogger:
    """Thread-safe in-memory activity log and execution state."""

    def __init__(self, maxlen: int = 300) -> None:
        self._lock = threading.RLock()
        self._maxlen = maxlen
        self._logs: deque[ActivityEntry] = deque(maxlen=maxlen)
        self._next_id = 1
        self._active_operation: dict[str, Any] = {
            "status": "idle",
            "action": None,
            "description": "System idle",
            "current_item": None,
            "progress_current": 0,
            "progress_total": 0,
            "started_at": None,
        }
        self._failed_tracks: dict[int, dict[str, Any]] = {}

        # Initial startup log
        self.log(
            level="info",
            category="system",
            message="Activity monitor initialized. Monitoring downloads and card sync.",
        )

    def log(
        self,
        level: str,
        category: str,
        message: str,
        track_id: int | None = None,
        track_title: str | None = None,
        artist: str | None = None,
        error: str | None = None,
    ) -> ActivityEntry:
        now = datetime.now()
        timestamp = now.strftime("%H:%M:%S")
        iso_time = now.astimezone(timezone.utc).isoformat()
        with self._lock:
            entry_id = self._next_id
            self._next_id += 1
            entry = ActivityEntry(
                id=entry_id,
                timestamp=timestamp,
                iso_time=iso_time,
                level=level,
                category=category,
                message=message,
                track_id=track_id,
                track_title=track_title,
                artist=artist,
                error=error,
            )
            self._logs.append(entry)
            if level == "error" and (track_id or track_title):
                key = track_id or hash(f"{track_title}:{artist}")
                self._failed_tracks[key] = {
                    "track_id": track_id,
                    "title": track_title or "Unknown",
                    "artist": artist or "Unknown",
                    "error": error or message,
                    "timestamp": timestamp,
                }
            elif level == "success" and track_id and track_id in self._failed_tracks:
                # Clear failure on subsequent success
                self._failed_tracks.pop(track_id, None)
            return entry

    def start_operation(
        self,
        action: str,
        description: str,
        total: int = 0,
    ) -> None:
        with self._lock:
            self._active_operation = {
                "status": "running",
                "action": action,
                "description": description,
                "current_item": None,
                "progress_current": 0,
                "progress_total": total,
                "started_at": datetime.now().strftime("%H:%M:%S"),
            }

    def update_progress(
        self,
        current: int,
        total: int | None = None,
        current_item: str | None = None,
    ) -> None:
        with self._lock:
            self._active_operation["progress_current"] = current
            if total is not None:
                self._active_operation["progress_total"] = total
            if current_item is not None:
                self._active_operation["current_item"] = current_item

    def finish_operation(self, summary: str | None = None) -> None:
        with self._lock:
            self._active_operation = {
                "status": "idle",
                "action": None,
                "description": summary or "System idle",
                "current_item": None,
                "progress_current": 0,
                "progress_total": 0,
                "started_at": None,
            }

    def clear_failures(self) -> None:
        with self._lock:
            self._failed_tracks.clear()

    def clear_logs(self) -> None:
        with self._lock:
            self._logs.clear()
            self._next_id = 1
            self.log(level="info", category="system", message="Activity log cleared by user.")

    def get_state(self, since_id: int = 0) -> dict[str, Any]:
        with self._lock:
            if since_id > 0:
                entries = [e.as_dict() for e in self._logs if e.id > since_id]
            else:
                entries = [e.as_dict() for e in self._logs]
            last_id = self._logs[-1].id if self._logs else since_id
            return {
                "logs": entries,
                "last_id": last_id,
                "active_operation": dict(self._active_operation),
                "failed_tracks": list(self._failed_tracks.values()),
            }


# Global singleton instance
activity = ActivityLogger()
