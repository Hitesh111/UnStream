"""Background automation: due-job scheduler plus optional launchd agent."""

from __future__ import annotations

import logging
import os
import plistlib
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from fiiolib.config import Config
from fiiolib.db import Database, utcnow
from fiiolib.discovery.engine import DiscoveryEngine
from fiiolib.downloads.manager import DownloadManager
from fiiolib.downloads.qbittorrent import QBittorrentClient
from fiiolib.library.organiser import Organiser
from fiiolib.library.reports import write_report
from fiiolib.library.scanner import LibraryScanner
from fiiolib.metadata.matcher import TrackMatcher
from fiiolib.metadata.tagger import read_tags
from fiiolib.spotify.auth import SpotifyAuth
from fiiolib.spotify.client import SpotifyClient
from fiiolib.spotify.sync import SpotifyImporter
from fiiolib.spotify.taste import TasteProfiler
from fiiolib.sync.planner import SyncPlanner

logger = logging.getLogger(__name__)

LAUNCHD_LABEL = "com.fiiolib.automation"
JOB_NAMES = (
    "spotify_sync",
    "discovery",
    "process_downloads",
    "library_scan",
    "integrity_check",
    "metadata_refresh",
    "backup",
    "card_sync",
)
MANUAL_SCHEDULES = {"manual", "off", "none", "on_detect_user_enabled"}


class SchedulerError(RuntimeError):
    pass


@dataclass
class JobOutcome:
    name: str
    status: str
    detail: dict[str, Any] = field(default_factory=dict)
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status,
            "detail": self.detail,
            "error": self.error,
        }


def parse_schedule(value: str) -> timedelta | None:
    value = (value or "").strip().lower()
    if not value or value in MANUAL_SCHEDULES:
        return None
    if value == "hourly":
        return timedelta(hours=1)
    if value == "daily":
        return timedelta(days=1)
    if value == "weekly":
        return timedelta(days=7)
    if value.endswith("m") and value[:-1].isdigit():
        return timedelta(minutes=int(value[:-1]))
    if value.endswith("h") and value[:-1].isdigit():
        return timedelta(hours=int(value[:-1]))
    if value.endswith("d") and value[:-1].isdigit():
        return timedelta(days=int(value[:-1]))
    raise SchedulerError(f"Unrecognised schedule {value!r} (use hourly, daily, weekly, 30m, 6h, 2d, manual)")


class Scheduler:
    def __init__(self, config: Config, db: Database) -> None:
        self.config = config
        self.db = db
        self.handlers: dict[str, Callable[[], dict[str, Any]]] = {
            "spotify_sync": self._spotify_sync,
            "discovery": self._discovery,
            "process_downloads": self._process_downloads,
            "library_scan": self._library_scan,
            "integrity_check": self._integrity_check,
            "metadata_refresh": self._metadata_refresh,
            "backup": self._backup,
            "card_sync": self._card_sync,
        }

    # ------------------------------------------------------------- due math
    def schedule_for(self, job: str) -> str:
        return str(getattr(self.config.automation, job, "manual"))

    def last_run(self, job: str) -> datetime | None:
        value = self.db.scalar(
            "SELECT MAX(finished_at) FROM workflow_runs WHERE workflow = ? AND status = 'completed'",
            (job,),
        )
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(str(value))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)

    def is_due(self, job: str, now: datetime | None = None) -> bool:
        interval = parse_schedule(self.schedule_for(job))
        if interval is None:
            return False
        now = now or datetime.now(UTC)
        last = self.last_run(job)
        if last is None:
            return True
        return now - last >= interval

    def due_jobs(self, now: datetime | None = None) -> list[str]:
        return [job for job in JOB_NAMES if self.is_due(job, now)]

    # ------------------------------------------------------------- execution
    def run_job(self, job: str) -> JobOutcome:
        if job not in self.handlers:
            raise SchedulerError(f"Unknown automation job {job!r}")
        run_id = self.db.start_workflow(job)
        try:
            detail = self.handlers[job]() or {}
            self.db.finish_workflow(run_id, "completed", detail)
            return JobOutcome(job, "completed", detail)
        except Exception as exc:
            logger.exception("Automation job %s failed", job)
            self.db.finish_workflow(run_id, "failed", {"error": str(exc)})
            return JobOutcome(job, "failed", {}, str(exc))

    def run_due(self) -> list[JobOutcome]:
        outcomes = []
        for job in self.due_jobs():
            outcomes.append(self.run_job(job))
        return outcomes

    # ------------------------------------------------------------- handlers
    def _spotify_sync(self) -> dict[str, Any]:
        auth = SpotifyAuth(self.config, self.db)
        if not auth.is_logged_in():
            raise SchedulerError("Spotify is not connected; run `musicctl spotify login`")
        client = SpotifyClient(self.config, self.db, auth=auth)
        try:
            stats = SpotifyImporter(self.config, self.db, client).run()
            profile = TasteProfiler(self.config, self.db).build()
        finally:
            client.close()
        return {"import": stats.as_dict(), "profile_artists": len(profile.get("artists") or [])}

    def _discovery(self) -> dict[str, Any]:
        spotify = None
        auth = SpotifyAuth(self.config, self.db)
        if auth.is_logged_in():
            spotify = SpotifyClient(self.config, self.db, auth=auth)
        engine = DiscoveryEngine(self.config, self.db, spotify=spotify)
        try:
            summary = engine.run()
        finally:
            engine.close()
            if spotify is not None:
                spotify.close()
        return summary.as_dict()

    def _process_downloads(self) -> dict[str, Any]:
        qbit = QBittorrentClient(self.config)
        manager = DownloadManager(self.config, self.db, qbit=qbit)
        try:
            if not qbit.is_available():
                raise SchedulerError("qBittorrent is not reachable")
            polled = manager.poll()
            processed = manager.process_downloaded()
        finally:
            qbit.close()
        return {"polled": len(polled), "processed": processed}

    def _library_scan(self) -> dict[str, Any]:
        summary = LibraryScanner(self.config, self.db).scan(compute_hashes=False)
        return summary.as_dict()

    def _integrity_check(self) -> dict[str, Any]:
        problems = LibraryScanner(self.config, self.db).integrity_check(deep=False)
        return {"problems": len(problems), "detail": problems[:50]}

    def _metadata_refresh(self) -> dict[str, Any]:
        scanner = LibraryScanner(self.config, self.db)
        summary = scanner.scan(compute_hashes=False)
        matcher = TrackMatcher(self.db)
        organiser = Organiser(self.config, self.db)
        matched = 0
        for row in self.db.query("SELECT id, path FROM files WHERE track_id IS NULL AND validated = 1"):
            path = Path(row["path"])
            if not path.exists():
                continue
            tags = read_tags(path)
            match = matcher.identify(tags)
            if match is None:
                continue
            self.db.execute(
                "UPDATE files SET track_id = ? WHERE id = ?", (match[0], row["id"])
            )
            if self.config.library.organize_on_verify and self.config.music_dir not in path.parents:
                organiser.organise(path, track_id=match[0])
            matched += 1
        return {"scanned": summary.as_dict(), "matched": matched}

    def _backup(self) -> dict[str, Any]:
        destination = self.db.backup(
            self.config.backups_dir / f"library-{utcnow().replace(':', '')}.sqlite3"
        )
        write_report(self.config, self.db, label="automation")
        return {"database": str(destination)}

    def _card_sync(self) -> dict[str, Any]:
        if not self.config.device.auto_sync:
            raise SchedulerError("Card auto-sync is disabled in config (device.auto_sync: false)")
        planner = SyncPlanner(self.config, self.db)
        plan = planner.plan(profile="complete")
        if not plan.fits:
            raise SchedulerError("Planned sync does not fit on the card")
        result = planner.execute(plan, dry_run=False, verify=True)
        return result

    # ------------------------------------------------------------- launchd
    def launchd_plist_path(self) -> Path:
        return Path.home() / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"

    def install_launchd(self, interval_minutes: int = 30) -> Path:
        path = self.launchd_plist_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        musicctl = Path(sys.executable).with_name("musicctl")
        arguments = [str(musicctl), "automation", "run"] if musicctl.exists() else [
            sys.executable,
            "-m",
            "fiiolib.cli.main",
            "automation",
            "run",
        ]
        payload = {
            "Label": LAUNCHD_LABEL,
            "ProgramArguments": arguments,
            "StartInterval": max(300, interval_minutes * 60),
            "RunAtLoad": False,
            "StandardOutPath": str(self.config.logs_dir / "automation.out.log"),
            "StandardErrorPath": str(self.config.logs_dir / "automation.err.log"),
            "EnvironmentVariables": {"PATH": os.environ.get("PATH", "/usr/bin:/bin:/usr/sbin:/sbin")},
        }
        path.write_bytes(plistlib.dumps(payload))
        _launchctl(["bootout", f"gui/{os.getuid()}/{LAUNCHD_LABEL}"], check=False)
        _launchctl(["bootstrap", f"gui/{os.getuid()}", str(path)], check=False)
        _launchctl(["enable", f"gui/{os.getuid()}/{LAUNCHD_LABEL}"], check=False)
        return path

    def uninstall_launchd(self) -> bool:
        path = self.launchd_plist_path()
        _launchctl(["bootout", f"gui/{os.getuid()}/{LAUNCHD_LABEL}"], check=False)
        if path.exists():
            path.unlink()
            return True
        return False


def _launchctl(arguments: list[str], check: bool = True) -> subprocess.CompletedProcess:
    try:
        completed = subprocess.run(
            ["launchctl", *arguments], capture_output=True, text=True, timeout=30, check=False
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise SchedulerError(f"launchctl failed: {exc}") from exc
    if check and completed.returncode != 0:
        raise SchedulerError(f"launchctl {' '.join(arguments)} failed: {completed.stderr.strip()}")
    return completed
