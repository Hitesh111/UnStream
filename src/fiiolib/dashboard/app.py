"""Local-only FastAPI dashboard (127.0.0.1, read-only JSON + static page + actions)."""

from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import FastAPI, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse

from fiiolib.automation.scheduler import JOB_NAMES, Scheduler
from fiiolib.config import Config
from fiiolib.db import Database
from fiiolib.device.detect import DeviceError, DeviceLocator
from fiiolib.discovery.engine import candidate_rows
from fiiolib.downloads.manager import DownloadManager
from fiiolib.library.queries import library_stats
from fiiolib.library.reports import report_payload
from fiiolib.library.scanner import LibraryScanner
from fiiolib.spotify.auth import SpotifyAuth
from fiiolib.spotify.client import SpotifyClient
from fiiolib.spotify.sync import SpotifyImporter, status_summary
from fiiolib.spotify.taste import PROFILE_KEY
from fiiolib.sync.planner import SyncPlanner

logger = logging.getLogger(__name__)

ALLOWED_HOSTS = {"127.0.0.1", "localhost", "::1"}


def create_app(config: Config, db: Database) -> FastAPI:
    app = FastAPI(title="FiiO Library Manager", docs_url=None, redoc_url=None)

    @app.middleware("http")
    async def local_only(request: Request, call_next):  # type: ignore[no-untyped-def]
        client = request.client.host if request.client else ""
        if client not in ALLOWED_HOSTS:
            return JSONResponse({"detail": "Local access only"}, status_code=403)
        host = (request.headers.get("host") or "").split(":")[0]
        if host and host not in ALLOWED_HOSTS:
            return JSONResponse({"detail": "Host not allowed"}, status_code=403)
        return await call_next(request)

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return INDEX_HTML

    @app.get("/api/status")
    def status() -> dict[str, Any]:
        return _status(config, db)

    @app.get("/api/library")
    def library() -> dict[str, Any]:
        return library_stats(db)

    @app.get("/api/report")
    def report() -> dict[str, Any]:
        return report_payload(config, db)

    @app.get("/api/candidates")
    def candidates(status_filter: str | None = Query(default=None, alias="status"), limit: int = 100) -> dict[str, Any]:
        rows = candidate_rows(db, status=status_filter, limit=min(limit, 500))
        return {"count": len(rows), "candidates": rows}

    @app.get("/api/devices")
    def devices() -> dict[str, Any]:
        try:
            volumes = DeviceLocator(config).candidates()
        except DeviceError as exc:
            return {"error": str(exc), "volumes": []}
        confirmed = {
            str(row["identifier"]): bool(row["confirmed"])
            for row in db.query("SELECT identifier, confirmed FROM devices")
        }
        payload = []
        for volume in volumes:
            item = volume.as_dict()
            item["confirmed"] = confirmed.get(item["identifier"], False)
            payload.append(item)
        return {"volumes": payload}

    @app.get("/api/sync")
    def sync_runs() -> dict[str, Any]:
        runs = [dict(row) for row in db.query("SELECT * FROM sync_runs ORDER BY id DESC LIMIT 20")]
        items = [dict(row) for row in db.query("SELECT status, COUNT(*) AS n FROM sync_items GROUP BY status")]
        return {"runs": runs, "item_status": items}

    @app.get("/api/downloads")
    def downloads() -> dict[str, Any]:
        acquisitions = [
            dict(row)
            for row in db.query(
                "SELECT a.id, a.status, a.updated_at, r.title, r.artist_name, r.licence "
                "FROM acquisitions a JOIN releases r ON r.id = a.release_id "
                "ORDER BY a.id DESC LIMIT 50"
            )
        ]
        jobs = [dict(row) for row in db.query("SELECT * FROM torrent_jobs ORDER BY id DESC LIMIT 20")]
        return {"acquisitions": acquisitions, "torrent_jobs": jobs}

    @app.get("/api/automation")
    def automation() -> dict[str, Any]:
        scheduler = Scheduler(config, db)
        return {
            "jobs": [
                {
                    "name": name,
                    "schedule": scheduler.schedule_for(name),
                    "last_run": scheduler.last_run(name).isoformat() if scheduler.last_run(name) else None,
                    "due": scheduler.is_due(name),
                }
                for name in JOB_NAMES
            ]
        }

    @app.get("/api/playlists")
    def list_playlists(
        q: str = Query(default=""),
        only_with_tracks: bool = Query(default=False),
        only_downloaded: bool = Query(default=False),
    ) -> dict[str, Any]:
        query = q.strip()
        sql = (
            "SELECT p.id, p.spotify_id, p.name, p.owner_name, p.description, p.total_tracks, "
            "p.public, p.collaborative, p.kind, "
            "COUNT(DISTINCT pt.track_id) AS indexed_tracks, "
            "COUNT(DISTINCT CASE WHEN f.id IS NOT NULL AND f.validated = 1 THEN pt.track_id END) AS downloaded_tracks "
            "FROM playlists p "
            "LEFT JOIN playlist_tracks pt ON pt.playlist_id = p.id "
            "LEFT JOIN files f ON f.track_id = pt.track_id "
        )
        params: list[Any] = []
        if query:
            sql += "WHERE (p.name LIKE ? OR p.owner_name LIKE ?) "
            params.extend([f"%{query}%", f"%{query}%"])
        sql += "GROUP BY p.id "
        having_clauses: list[str] = []
        if only_with_tracks:
            having_clauses.append("indexed_tracks > 0")
        if only_downloaded:
            having_clauses.append("downloaded_tracks > 0")
        if having_clauses:
            sql += "HAVING " + " AND ".join(having_clauses) + " "
        sql += "ORDER BY (CASE WHEN p.owner_name = 'Hitesh' THEN 0 ELSE 1 END), indexed_tracks DESC, p.name COLLATE NOCASE ASC"
        rows = [dict(row) for row in db.query(sql, tuple(params))]
        return {"count": len(rows), "playlists": rows}

    @app.get("/api/playlists/{playlist_id}/tracks")
    def playlist_tracks(playlist_id: int) -> Any:
        pl_row = db.one("SELECT * FROM playlists WHERE id = ?", (playlist_id,))
        if not pl_row:
            return JSONResponse({"error": "Playlist not found"}, status_code=404)
        tracks = [
            dict(row)
            for row in db.query(
                "SELECT pt.position, t.id, t.title, a.name AS artist_name, al.title AS album_title, "
                "t.track_number, t.duration_ms, f.validated, f.path "
                "FROM playlist_tracks pt "
                "JOIN tracks t ON t.id = pt.track_id "
                "JOIN artists a ON a.id = t.artist_id "
                "JOIN albums al ON al.id = t.album_id "
                "LEFT JOIN files f ON f.track_id = t.id "
                "WHERE pt.playlist_id = ? "
                "ORDER BY pt.position ASC, t.id ASC",
                (playlist_id,),
            )
        ]
        return {
            "playlist": dict(pl_row),
            "count": len(tracks),
            "tracks": tracks,
        }

    @app.get("/api/songs")
    def list_songs(
        q: str = Query(default=""),
        category: str = Query(default="all"),
        only_downloaded: bool = Query(default=False),
        page: int = Query(default=1, ge=1),
        page_size: int = Query(default=50, ge=1, le=200),
    ) -> dict[str, Any]:
        query = q.strip()
        conditions: list[str] = []
        params: list[Any] = []

        join_clause = (
            "FROM tracks t "
            "JOIN artists a ON a.id = t.artist_id "
            "JOIN albums al ON al.id = t.album_id "
            "LEFT JOIN files f ON f.track_id = t.id "
        )

        if category == "saved":
            join_clause += "JOIN spotify_saved_tracks st ON st.track_id = t.id "
        elif category in ("synced", "downloaded"):
            conditions.append("f.validated = 1")

        if only_downloaded:
            conditions.append("f.validated = 1")

        if query:
            pattern = f"%{query}%"
            conditions.append("(t.title LIKE ? OR a.name LIKE ? OR al.title LIKE ?)")
            params.extend([pattern, pattern, pattern])

        where_sql = ("WHERE " + " AND ".join(conditions)) if conditions else ""

        count_sql = f"SELECT COUNT(DISTINCT t.id) {join_clause} {where_sql}"
        total_count = int(db.scalar(count_sql, tuple(params)) or 0)

        offset = (page - 1) * page_size
        data_sql = (
            f"SELECT t.id, t.title, a.name AS artist_name, al.title AS album_title, "
            f"t.track_number, t.duration_ms, f.validated, f.path "
            f"{join_clause} {where_sql} "
            f"GROUP BY t.id "
            f"ORDER BY a.name COLLATE NOCASE ASC, al.title COLLATE NOCASE ASC, t.track_number ASC "
            f"LIMIT ? OFFSET ?"
        )
        data_params = list(params) + [page_size, offset]
        rows = [dict(row) for row in db.query(data_sql, tuple(data_params))]
        total_pages = max(1, (total_count + page_size - 1) // page_size)
        return {
            "total": total_count,
            "page": page,
            "page_size": page_size,
            "total_pages": total_pages,
            "songs": rows,
        }

    @app.get("/api/downloaded-files")
    def list_downloaded_files(q: str = Query(default="")) -> dict[str, Any]:
        query = q.strip()
        sql = (
            "SELECT f.id, f.path, f.format, f.size_bytes, f.sha256, "
            "t.id AS track_id, t.title, a.name AS artist_name, al.title AS album_title, "
            "t.duration_ms, f.validated, f.mtime "
            "FROM files f "
            "JOIN tracks t ON t.id = f.track_id "
            "JOIN artists a ON a.id = t.artist_id "
            "JOIN albums al ON al.id = t.album_id "
            "WHERE f.validated = 1 "
        )
        params: list[Any] = []
        if query:
            pattern = f"%{query}%"
            sql += "AND (t.title LIKE ? OR a.name LIKE ? OR al.title LIKE ? OR f.path LIKE ?) "
            params.extend([pattern, pattern, pattern, pattern])
        sql += "ORDER BY a.name COLLATE NOCASE ASC, al.title COLLATE NOCASE ASC, t.track_number ASC"
        rows = [dict(row) for row in db.query(sql, tuple(params))]
        total_bytes = sum(int(r["size_bytes"] or 0) for r in rows)
        return {
            "count": len(rows),
            "total_bytes": total_bytes,
            "files": rows,
        }

    @app.get("/api/search")
    def search(q: str = Query(default="")) -> dict[str, Any]:
        query = q.strip()
        if not query:
            return {"query": "", "playlists": [], "tracks": [], "artists": [], "candidates": []}
        pattern = f"%{query}%"
        playlists = [
            dict(row)
            for row in db.query(
                "SELECT p.id, p.name, p.owner_name, p.total_tracks, COUNT(pt.track_id) AS indexed_tracks "
                "FROM playlists p "
                "LEFT JOIN playlist_tracks pt ON pt.playlist_id = p.id "
                "WHERE p.name LIKE ? OR p.owner_name LIKE ? "
                "GROUP BY p.id ORDER BY (CASE WHEN p.owner_name = 'Hitesh' THEN 0 ELSE 1 END), indexed_tracks DESC, p.name ASC LIMIT 15",
                (pattern, pattern),
            )
        ]
        tracks = [
            dict(row)
            for row in db.query(
                "SELECT t.id, t.title, a.name AS artist_name, al.title AS album_title, "
                "t.track_number, t.duration_ms, f.validated, f.path "
                "FROM tracks t "
                "JOIN artists a ON a.id = t.artist_id "
                "JOIN albums al ON al.id = t.album_id "
                "LEFT JOIN files f ON f.track_id = t.id "
                "WHERE t.title LIKE ? OR a.name LIKE ? OR al.title LIKE ? "
                "ORDER BY (f.validated IS NOT NULL AND f.validated = 1) DESC, t.id ASC LIMIT 40",
                (pattern, pattern, pattern),
            )
        ]
        artists = [
            dict(row)
            for row in db.query(
                "SELECT a.id, a.name, COUNT(t.id) AS tracks_count "
                "FROM artists a "
                "LEFT JOIN tracks t ON t.artist_id = a.id "
                "WHERE a.name LIKE ? "
                "GROUP BY a.id ORDER BY tracks_count DESC LIMIT 20",
                (pattern,),
            )
        ]
        candidates = [
            dict(row)
            for row in db.query(
                "SELECT c.*, ar.name AS artist_name FROM discovery_candidates c "
                "LEFT JOIN artists ar ON ar.id = c.artist_id "
                "WHERE c.entity_name LIKE ? OR ar.name LIKE ? "
                "ORDER BY c.score DESC LIMIT 25",
                (pattern, pattern),
            )
        ]
        for c in candidates:
            try:
                c["reasons"] = json.loads(c.get("reasons") or "[]")
            except Exception:
                c["reasons"] = []
        return {"query": query, "playlists": playlists, "tracks": tracks, "artists": artists, "candidates": candidates}

    @app.post("/api/actions/sync")
    def action_sync(dry_run: bool = Query(default=True)) -> dict[str, Any]:
        from fiiolib.activity import activity
        try:
            planner = SyncPlanner(config, db)
            plan = planner.plan()
            if not dry_run and plan.fits:
                result = planner.execute(plan, dry_run=False)
                return {"ok": True, "action": "sync", "result": result}
            else:
                total_write = len([i for i in plan.items if i.action in {"add", "update"}])
                activity.log(
                    level="info",
                    category="sync",
                    message=f"Sync preview: {len(plan.items)} tracks checked ({total_write} to write, {len([i for i in plan.items if i.action == 'skip'])} already verified on card)",
                )
            return {"ok": True, "action": "plan", "plan": plan.as_dict()}
        except Exception as exc:
            logger.exception("Dashboard sync action failed")
            activity.log(level="error", category="sync", message=f"Sync plan failed: {exc}", error=str(exc))
            return {"ok": False, "error": str(exc)}

    @app.post("/api/actions/scan")
    def action_scan() -> dict[str, Any]:
        from fiiolib.activity import activity
        try:
            activity.log(level="info", category="system", message="Starting library scan of audio files…")
            scanner = LibraryScanner(config, db)
            summary = scanner.scan()
            activity.log(
                level="success",
                category="system",
                message=f"Library scan complete: {summary.scanned} files inspected, {summary.new} new, {summary.errors} errors",
            )
            return {"ok": True, "summary": summary.as_dict()}
        except Exception as exc:
            logger.exception("Dashboard scan action failed")
            activity.log(level="error", category="system", message=f"Library scan error: {exc}", error=str(exc))
            return {"ok": False, "error": str(exc)}

    @app.post("/api/actions/approve")
    def action_approve(candidate_id: int = Query(...)) -> dict[str, Any]:
        try:
            manager = DownloadManager(config, db)
            res = manager.approve(candidate_id)
            return {"ok": True, "result": res}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    @app.post("/api/actions/reject")
    def action_reject(candidate_id: int = Query(...)) -> dict[str, Any]:
        try:
            manager = DownloadManager(config, db)
            res = manager.reject(candidate_id)
            return {"ok": True, "result": res}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    @app.post("/api/actions/bulk-approve")
    def action_bulk_approve(ids: str = Query(...)) -> dict[str, Any]:
        manager = DownloadManager(config, db)
        candidate_ids = [int(i.strip()) for i in ids.split(",") if i.strip().isdigit()]
        results = []
        for cid in candidate_ids:
            try:
                res = manager.approve(cid)
                results.append({"id": cid, "ok": True, "result": res})
            except Exception as exc:
                results.append({"id": cid, "ok": False, "error": str(exc)})
        return {"ok": True, "count": len(results), "results": results}

    @app.post("/api/actions/bulk-reject")
    def action_bulk_reject(ids: str = Query(...)) -> dict[str, Any]:
        manager = DownloadManager(config, db)
        candidate_ids = [int(i.strip()) for i in ids.split(",") if i.strip().isdigit()]
        results = []
        for cid in candidate_ids:
            try:
                res = manager.reject(cid)
                results.append({"id": cid, "ok": True, "result": res})
            except Exception as exc:
                results.append({"id": cid, "ok": False, "error": str(exc)})
        return {"ok": True, "count": len(results), "results": results}

    @app.post("/api/actions/approve-all")
    def action_approve_all() -> dict[str, Any]:
        manager = DownloadManager(config, db)
        rows = db.query("SELECT id FROM discovery_candidates WHERE status = 'awaiting_approval'")
        results = []
        for row in rows:
            cid = int(row["id"])
            try:
                res = manager.approve(cid)
                results.append({"id": cid, "ok": True, "result": res})
            except Exception as exc:
                results.append({"id": cid, "ok": False, "error": str(exc)})
        return {"ok": True, "count": len(results), "results": results}

    @app.post("/api/actions/download-track")
    def action_download_track(track_id: int = Query(...)) -> dict[str, Any]:
        from fiiolib.downloads.downloader import download_track
        return download_track(config, db, track_id)

    @app.post("/api/actions/download-playlist")
    def action_download_playlist(playlist_id: int = Query(...), max_tracks: int = Query(default=30)) -> dict[str, Any]:
        from fiiolib.downloads.downloader import download_playlist
        return download_playlist(config, db, playlist_id, max_tracks=max_tracks)

    @app.post("/api/actions/download-playlists-batch")
    def action_download_playlists_batch(
        playlist_ids: str = Query(...),
        max_tracks_per_playlist: int = Query(default=30),
    ) -> dict[str, Any]:
        from fiiolib.downloads.downloader import download_playlists_batch
        pids = [int(x.strip()) for x in playlist_ids.split(",") if x.strip().isdigit()]
        if not pids:
            return {"ok": False, "error": "No valid playlist IDs provided"}
        return download_playlists_batch(config, db, pids, max_tracks_per_playlist=max_tracks_per_playlist)

    @app.post("/api/actions/download-all-playlists")
    def action_download_all_playlists(
        max_tracks_per_playlist: int = Query(default=30),
    ) -> dict[str, Any]:
        from fiiolib.downloads.downloader import download_all_playlists
        return download_all_playlists(config, db, max_tracks_per_playlist=max_tracks_per_playlist)

    @app.post("/api/actions/export-playlists")
    def action_export_playlists() -> dict[str, Any]:
        from fiiolib.downloads.downloader import export_all_playlists
        exported = export_all_playlists(config, db)
        return {"ok": True, "exported": len(exported), "playlists": exported}

    @app.post("/api/actions/download-saved")
    def action_download_saved(limit: int = Query(default=20)) -> dict[str, Any]:
        from fiiolib.downloads.downloader import download_saved_tracks
        return download_saved_tracks(config, db, limit=limit)

    @app.post("/api/actions/spotify-sync")
    def action_spotify_sync() -> dict[str, Any]:
        auth = SpotifyAuth(config, db)
        if not auth.is_logged_in():
            return {"ok": False, "error": "Spotify not logged in"}
        client = SpotifyClient(config, db, auth=auth)
        try:
            importer = SpotifyImporter(config, db, client)
            summary = importer.run_incremental()
            return {"ok": True, "summary": summary}
        except Exception as exc:
            logger.exception("Dashboard Spotify sync failed")
            return {"ok": False, "error": str(exc)}
        finally:
            client.close()

    @app.get("/api/activity")
    def get_activity_logs(since_id: int = Query(default=0)) -> dict[str, Any]:
        from fiiolib.activity import activity
        return activity.get_state(since_id=since_id)

    @app.post("/api/actions/activity-clear")
    def clear_activity_logs() -> dict[str, Any]:
        from fiiolib.activity import activity
        activity.clear_logs()
        return {"ok": True}

    @app.post("/api/actions/activity-clear-failures")
    def clear_activity_failures() -> dict[str, Any]:
        from fiiolib.activity import activity
        activity.clear_failures()
        return {"ok": True}

    @app.exception_handler(DeviceError)
    def device_error(_: Request, exc: DeviceError) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=409)

    @app.get("/api/health")
    def health() -> dict[str, Any]:
        return {"ok": True, "database": db.integrity_check()["ok"]}

    return app


def _status(config: Config, db: Database) -> dict[str, Any]:
    capabilities: dict[str, Any] = {}
    try:
        volumes = DeviceLocator(config).candidates()
        confirmed = {
            str(row["identifier"]): bool(row["confirmed"])
            for row in db.query("SELECT identifier, confirmed FROM devices")
        }
        capabilities["devices"] = [
            {**volume.as_dict(), "confirmed": confirmed.get(volume.identifier, False)}
            for volume in volumes
        ]
    except DeviceError as exc:
        capabilities["devices"] = []
        capabilities["device_error"] = str(exc)
    spotify = status_summary(db)
    spotify["logged_in"] = SpotifyAuth(config, db).is_logged_in()
    spotify["profile_artists"] = len((db.get_preference(PROFILE_KEY) or {}).get("artists") or [])
    capabilities["spotify"] = spotify
    capabilities["library"] = library_stats(db)
    capabilities["counts"] = {
        "candidates": int(db.scalar("SELECT COUNT(*) FROM discovery_candidates") or 0),
        "awaiting_approval": int(
            db.scalar("SELECT COUNT(*) FROM discovery_candidates WHERE status = 'awaiting_approval'") or 0
        ),
        "acquisitions": int(db.scalar("SELECT COUNT(*) FROM acquisitions") or 0),
        "sync_runs": int(db.scalar("SELECT COUNT(*) FROM sync_runs") or 0),
        "files": int(db.scalar("SELECT COUNT(*) FROM files") or 0),
        "downloaded_files": int(db.scalar("SELECT COUNT(*) FROM files WHERE validated = 1") or 0),
        "playlists": int(db.scalar("SELECT COUNT(*) FROM playlists") or 0),
        "saved_tracks": int(db.scalar("SELECT COUNT(*) FROM spotify_saved_tracks") or 0),
        "tracks": int(db.scalar("SELECT COUNT(*) FROM tracks") or 0),
    }
    return capabilities


INDEX_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>FiiO Echo Mini — Library Manager</title>
<style>
:root {
  color-scheme: dark;
  --bg: #090d13;
  --panel: #111722;
  --panel-hover: #161e2c;
  --line: #222c3d;
  --line-light: #2d3a50;
  --fg: #e2e8f0;
  --muted: #8b9bb4;
  --accent: #38bdf8;
  --accent-hover: #0284c7;
  --good: #34d399;
  --warn: #fbbf24;
  --bad: #f87171;
}
* { box-sizing: border-box; }
body {
  margin: 0;
  font: 13px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
  background: var(--bg);
  color: var(--fg);
}
header {
  padding: 16px 28px;
  background: #0d131d;
  border-bottom: 1px solid var(--line);
  display: flex;
  justify-content: space-between;
  align-items: center;
  flex-wrap: wrap;
  gap: 12px;
}
.brand { display: flex; align-items: baseline; gap: 12px; }
.brand h1 { margin: 0; font-size: 16px; font-weight: 600; letter-spacing: -0.01em; color: #fff; }
.brand span { color: var(--muted); font-size: 12px; }
.header-actions { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
button {
  background: #1e293b;
  border: 1px solid var(--line-light);
  color: var(--fg);
  padding: 6px 12px;
  border-radius: 6px;
  font-size: 12px;
  font-weight: 500;
  cursor: pointer;
  transition: all 0.15s ease;
  display: inline-flex;
  align-items: center;
  gap: 6px;
}
button:hover { background: #334155; border-color: var(--accent); }
button.primary { background: #0284c7; border-color: #38bdf8; color: #fff; }
button.primary:hover { background: #0369a1; }
button.success { background: rgba(52, 211, 153, 0.2); border-color: #34d399; color: #34d399; }
button.success:hover { background: rgba(52, 211, 153, 0.35); }
button.danger { background: rgba(248, 113, 113, 0.2); border-color: #f87171; color: #f87171; }
button.danger:hover { background: rgba(248, 113, 113, 0.35); }
button:disabled { opacity: 0.5; cursor: not-allowed; }

.search-bar-wrap {
  padding: 14px 28px;
  background: #0c121c;
  border-bottom: 1px solid var(--line);
  display: flex;
  gap: 12px;
  align-items: center;
}
.search-input-box {
  flex: 1;
  max-width: 650px;
  position: relative;
}
.search-input-box input {
  width: 100%;
  background: #141c2b;
  border: 1px solid var(--line-light);
  border-radius: 6px;
  padding: 8px 12px 8px 34px;
  color: #fff;
  font-size: 13px;
  outline: none;
}
.search-input-box input:focus {
  border-color: var(--accent);
  box-shadow: 0 0 0 2px rgba(56, 189, 248, 0.15);
}
.search-icon {
  position: absolute;
  left: 11px;
  top: 8px;
  color: var(--muted);
  font-size: 14px;
}

#toast {
  padding: 10px 16px;
  background: #1e293b;
  border-left: 3px solid var(--accent);
  margin: 12px 28px 0;
  border-radius: 4px;
  font-size: 12px;
  display: none;
}
#search-results-panel {
  display: none;
  margin: 14px 28px 0;
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: 8px;
  padding: 16px 20px;
}

main {
  padding: 20px 28px;
  display: grid;
  gap: 18px;
  grid-template-columns: repeat(auto-fit, minmax(320px, 1fr));
}
section {
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: 8px;
  padding: 16px 18px;
  display: flex;
  flex-direction: column;
}
section.full-width {
  grid-column: 1 / -1;
}
.sec-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 12px;
  border-bottom: 1px solid var(--line);
  padding-bottom: 8px;
  flex-wrap: wrap;
  gap: 8px;
}
.sec-head-actions {
  display: flex;
  align-items: center;
  gap: 8px;
}
h2 {
  margin: 0;
  font-size: 11px;
  text-transform: uppercase;
  letter-spacing: 0.08em;
  color: var(--muted);
  font-weight: 600;
}
.kv {
  display: flex;
  justify-content: space-between;
  gap: 12px;
  padding: 5px 0;
  border-bottom: 1px dashed rgba(255,255,255,0.05);
}
.kv:last-child { border: 0; }
.kv b { font-weight: 600; font-variant-numeric: tabular-nums; }
table {
  width: 100%;
  border-collapse: collapse;
  font-size: 12px;
  font-variant-numeric: tabular-nums;
}
td, th {
  text-align: left;
  padding: 7px 8px;
  border-bottom: 1px solid #192233;
}
th {
  color: var(--muted);
  font-weight: 500;
  font-size: 11px;
  text-transform: uppercase;
  letter-spacing: 0.04em;
}
tr:hover td { background: rgba(255,255,255,0.02); }
.badge {
  display: inline-block;
  padding: 2px 7px;
  border-radius: 4px;
  font-size: 10px;
  font-weight: 600;
  letter-spacing: 0.02em;
}
.badge.ok { background: rgba(52, 211, 153, 0.15); color: var(--good); border: 1px solid rgba(52, 211, 153, 0.3); }
.badge.warn { background: rgba(251, 191, 36, 0.15); color: var(--warn); border: 1px solid rgba(251, 191, 36, 0.3); }
.badge.bad { background: rgba(248, 113, 113, 0.15); color: var(--bad); border: 1px solid rgba(248, 113, 113, 0.3); }
.badge.sync { background: rgba(56, 189, 248, 0.15); color: var(--accent); border: 1px solid rgba(56, 189, 248, 0.3); }
.ok { color: var(--good); }
.warn { color: var(--warn); }
.bad { color: var(--bad); }
footer {
  padding: 12px 28px 32px;
  color: var(--muted);
  font-size: 11px;
  display: flex;
  justify-content: space-between;
}
.btn-sm { padding: 4px 8px; font-size: 11px; border-radius: 4px; }
.empty-msg { color: var(--muted); font-size: 12px; padding: 14px 0; font-style: italic; }
input[type="checkbox"] { cursor: pointer; }

.tabs-nav {
  display: flex;
  gap: 4px;
  background: #0c121c;
  padding: 3px;
  border-radius: 6px;
  border: 1px solid var(--line);
}
.tab-btn {
  background: transparent;
  border: 0;
  color: var(--muted);
  padding: 4px 10px;
  font-size: 11px;
  border-radius: 4px;
  cursor: pointer;
  font-weight: 500;
  transition: all 0.15s ease;
}
.tab-btn:hover { color: #fff; background: rgba(255,255,255,0.05); }
.tab-btn.active { color: #fff; background: #1e293b; font-weight: 600; border: 1px solid var(--line-light); }
.clickable-row { cursor: pointer; }
.clickable-row:hover td { background: rgba(56, 189, 248, 0.05) !important; }
.modal-overlay {
  display: none;
  position: fixed;
  top: 0;
  left: 0;
  width: 100%;
  height: 100%;
  background: rgba(0, 0, 0, 0.75);
  backdrop-filter: blur(4px);
  z-index: 9999;
  align-items: center;
  justify-content: center;
}
.modal-dialog {
  background: #0f1624;
  border: 1px solid var(--line-light);
  border-radius: 10px;
  width: 92%;
  max-width: 950px;
  max-height: 85vh;
  display: flex;
  flex-direction: column;
  box-shadow: 0 20px 40px rgba(0, 0, 0, 0.6);
  animation: fadeIn 0.15s ease-out;
}
@keyframes fadeIn {
  from { opacity: 0; transform: scale(0.98); }
  to { opacity: 1; transform: scale(1); }
}

/* Live Activity & Log Monitor */
.log-row {
  display: flex;
  align-items: baseline;
  gap: 8px;
  margin-bottom: 3px;
  line-height: 1.5;
}
.log-time {
  color: #64748b;
  font-size: 11px;
  font-variant-numeric: tabular-nums;
  flex-shrink: 0;
}
.log-cat {
  color: #818cf8;
  font-size: 11px;
  font-weight: 600;
  flex-shrink: 0;
}
.log-badge {
  font-size: 9px;
  padding: 1px 5px;
  border-radius: 3px;
  font-weight: 700;
  text-transform: uppercase;
  flex-shrink: 0;
  letter-spacing: 0.04em;
}
.log-badge.info { background: rgba(56, 189, 248, 0.15); color: #38bdf8; }
.log-badge.success { background: rgba(52, 211, 153, 0.15); color: #34d399; }
.log-badge.warn { background: rgba(251, 191, 36, 0.15); color: #fbbf24; }
.log-badge.error { background: rgba(248, 113, 113, 0.2); color: #f87171; }
.log-msg {
  color: #cbd5e1;
  word-break: break-word;
}
.log-msg.success { color: #86efac; font-weight: 500; }
.log-msg.error { color: #fca5a5; font-weight: 500; }
.log-msg.warn { color: #fde047; }
@keyframes pulseGlow {
  0% { opacity: 0.5; }
  50% { opacity: 1; }
  100% { opacity: 0.5; }
}
.pulsing-dot {
  display: inline-block;
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: var(--muted);
  margin-right: 5px;
}
.pulsing-dot.active {
  background: var(--accent);
  animation: pulseGlow 1.2s infinite ease-in-out;
}
.pulsing-dot.ready {
  background: var(--good);
}
.failed-track-card {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 8px 12px;
  background: rgba(248, 113, 113, 0.06);
  border: 1px solid rgba(248, 113, 113, 0.25);
  border-radius: 5px;
  font-size: 12px;
  gap: 12px;
  flex-wrap: wrap;
}
</style>
</head>
<body>
<header>
  <div class="brand">
    <h1>FiiO SNOWSKY Echo Mini</h1>
    <span>Library Manager & Sync</span>
    <span id="stamp" style="font-size: 11px;">connecting…</span>
  </div>
  <div class="header-actions">
    <button onclick="triggerScan(this)">🔍 Scan Library</button>
    <button onclick="triggerSync(this, true)">📋 Preview Sync</button>
    <button class="primary" onclick="triggerSync(this, false)">⚡ Sync Card Now</button>
    <button onclick="refreshAll()">↻ Refresh</button>
  </div>
</header>

<!-- Global Live Search Bar -->
<div class="search-bar-wrap">
  <div class="search-input-box">
    <span class="search-icon">🔍</span>
    <input type="text" id="global-search" placeholder="Search artists, songs, albums, and discovery candidates…" oninput="onSearchInput(this.value)">
  </div>
  <span style="color:var(--muted); font-size:12px;" id="search-hint">Type to search across Spotify library & candidates</span>
</div>

<!-- Search Results Dropdown/Panel -->
<div id="search-results-panel">
  <div class="sec-head">
    <h2 id="search-results-title">Search Results</h2>
    <button class="btn-sm" onclick="closeSearch()">✕ Close</button>
  </div>
  <div id="search-results-content"></div>
</div>

<div id="toast"></div>

<main>
  <!-- Library Status -->
  <section>
    <div class="sec-head"><h2>Verified Library</h2><span id="lib-status" class="badge ok">local</span></div>
    <div id="library">Loading…</div>
  </section>

  <!-- Spotify Connection -->
  <section>
    <div class="sec-head">
      <h2>Spotify Taste Profile</h2>
      <button class="btn-sm" onclick="triggerSpotifySync(this)">Sync Spotify</button>
    </div>
    <div id="spotify">Loading…</div>
  </section>

  <!-- Target MicroSD Device -->
  <section>
    <div class="sec-head"><h2>Target MicroSD Card</h2><span id="card-badge" class="badge">detecting</span></div>
    <div id="devices">Loading…</div>
  </section>

  <!-- Pipeline Counts -->
  <section>
    <div class="sec-head"><h2>Pipeline & Safety</h2></div>
    <div id="pipeline">Loading…</div>
  </section>

  <!-- Live Activity, Sync & Download Execution Monitor -->
  <section class="full-width" id="activity-monitor-section">
    <div class="sec-head">
      <div style="display:flex; align-items:center; gap:12px; flex-wrap:wrap;">
        <h2>Live Activity & Execution Monitor</h2>
        <span id="activity-status-badge" class="badge ok"><span class="pulsing-dot ready" id="activity-pulse"></span>Idle</span>
        <span id="activity-task-desc" style="font-size:12px; color:var(--fg); font-weight:500;">Ready for acquisition and sync</span>
      </div>
      <div class="sec-head-actions">
        <button class="btn-sm" id="btn-autoscroll" onclick="toggleAutoScroll()">Auto-scroll: ON</button>
        <button class="btn-sm" onclick="clearActivityLogs()">Clear Console</button>
      </div>
    </div>

    <!-- Active Task Progress Bar -->
    <div id="activity-progress-wrap" style="display:none; margin-bottom:12px; padding:10px 14px; background:#0c121c; border:1px solid var(--line); border-radius:6px;">
      <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:6px; flex-wrap:wrap; gap:8px;">
        <span id="activity-progress-label" style="font-size:12px; font-weight:600; color:var(--accent);">Processing…</span>
        <span id="activity-progress-ratio" style="font-size:11px; color:var(--muted); font-variant-numeric:tabular-nums;">0 / 0</span>
      </div>
      <div style="width:100%; height:4px; background:#1e293b; border-radius:2px; overflow:hidden;">
        <div id="activity-progress-bar" style="width:0%; height:100%; background:var(--accent); transition:width 0.25s ease;"></div>
      </div>
    </div>

    <!-- Failed Tracks Alert & Retry Box -->
    <div id="activity-failures-panel" style="display:none; margin-bottom:12px; padding:12px 14px; background:rgba(248,113,113,0.08); border:1px solid rgba(248,113,113,0.3); border-radius:6px;">
      <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:8px;">
        <span style="font-size:12px; font-weight:600; color:var(--bad); display:flex; align-items:center; gap:6px;">
          <span>⚠️</span> <span id="failures-count-label">0 Failed Tracks</span>
        </span>
        <button class="btn-sm" onclick="clearActivityFailures()" style="border-color:rgba(248,113,113,0.4); color:var(--bad);">✕ Dismiss All Failures</button>
      </div>
      <div id="failures-list" style="display:flex; flex-direction:column; gap:6px;"></div>
    </div>

    <!-- Scrollable Terminal Log Console -->
    <div id="activity-console" style="height:230px; overflow-y:auto; background:#070a0f; border:1px solid var(--line); border-radius:6px; padding:12px 14px; font-family:ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace; font-size:11.5px; line-height:1.6; color:#94a3b8;">
      <div class="empty-msg">Waiting for activity events…</div>
    </div>
  </section>

  <!-- Spotify Library Browser (Playlists & Songs & Downloaded Local) -->
  <section class="full-width" id="spotify-browser-section">
    <div class="sec-head">
      <div style="display:flex; align-items:center; gap:16px; flex-wrap:wrap;">
        <h2>Spotify Library & Playlists</h2>
        <div class="tabs-nav" id="browser-tabs">
          <button class="tab-btn active" id="tab-btn-playlists" onclick="switchBrowserTab('playlists')">Playlists (<span id="pl-tab-count">110</span>)</button>
          <button class="tab-btn" id="tab-btn-saved" onclick="switchBrowserTab('saved')">Liked Songs (<span id="saved-tab-count">372</span>)</button>
          <button class="tab-btn" id="tab-btn-all_songs" onclick="switchBrowserTab('all_songs')">All Songs (<span id="all-songs-tab-count">2,028</span>)</button>
          <button class="tab-btn" id="tab-btn-downloaded" onclick="switchBrowserTab('downloaded')">Downloaded Local (<span id="downloaded-tab-count">6</span>)</button>
        </div>
      </div>
      <div class="sec-head-actions">
        <input type="text" id="browser-filter-input" placeholder="Filter playlists by name or curator…" oninput="onBrowserFilter(this.value)" style="background:#141c2b; border:1px solid var(--line-light); border-radius:4px; padding:5px 10px; font-size:12px; color:#fff; width:260px; outline:none;">
      </div>
    </div>

    <!-- Playlists View -->
    <div id="tab-view-playlists">
      <div style="margin-bottom: 10px; display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:8px;">
        <div style="display:flex; align-items:center; gap:10px; flex-wrap:wrap;">
          <span style="color:var(--muted); font-size:11px;">Select playlists to batch download or export to FiiO microSD card.</span>
          <span id="playlist-selected-counter" style="font-size: 11px; color: var(--accent); font-weight: 600;"></span>
        </div>
        <div style="display:flex; align-items:center; gap:8px; flex-wrap:wrap;">
          <button class="btn-sm primary" id="btn-dl-selected-playlists" onclick="downloadSelectedPlaylists(this)" disabled>⬇ Download Selected</button>
          <button class="btn-sm" id="btn-dl-all-playlists" onclick="downloadAllPlaylists(this)">⬇ Download All Playlists</button>
          <button class="btn-sm" onclick="exportAllM3uPlaylists(this)">📑 Export M3U8s</button>
          <label style="font-size:11px; color:var(--muted); cursor:pointer; display:flex; align-items:center; gap:5px; margin-left:6px;">
            <input type="checkbox" id="chk-only-indexed" onchange="loadPlaylists()" checked> Only with tracks
          </label>
          <label style="font-size:11px; color:var(--muted); cursor:pointer; display:flex; align-items:center; gap:5px;">
            <input type="checkbox" id="chk-only-downloaded-pl" onchange="loadPlaylists()"> Only downloaded
          </label>
        </div>
      </div>
      <div style="max-height: 480px; overflow-y: auto; border: 1px solid var(--line); border-radius: 6px;">
        <table id="playlists-table">
          <thead>
            <tr>
              <th style="width: 32px;"><input type="checkbox" id="select-all-playlists" onchange="toggleSelectAllPlaylists(this)" title="Select/Deselect All Playlists"></th>
              <th>Playlist Name</th>
              <th>Curator / Owner</th>
              <th style="text-align:right;">Downloaded</th>
              <th style="text-align:right;">Indexed</th>
              <th style="text-align:right;">Total</th>
              <th>Status</th>
              <th style="width: 140px;">Action</th>
            </tr>
          </thead>
          <tbody id="playlists-body">
            <tr><td colspan="8" class="empty-msg">Loading playlists…</td></tr>
          </tbody>
        </table>
      </div>
    </div>

    <!-- Songs View (Liked Songs & All Songs) -->
    <div id="tab-view-songs" style="display:none;">
      <div style="margin-bottom: 10px; display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:8px;">
        <div style="display:flex; align-items:center; gap:12px; flex-wrap:wrap;">
          <span id="songs-count-label" style="color:var(--muted); font-size:11px;">Showing songs from your library</span>
          <label style="font-size:11px; color:var(--muted); cursor:pointer; display:flex; align-items:center; gap:5px;">
            <input type="checkbox" id="chk-song-downloaded-only" onchange="onSongDownloadedFilterChange()"> Only show downloaded (local disk)
          </label>
        </div>
        <div style="display:flex; align-items:center; gap:8px;">
          <button class="btn-sm primary" id="btn-dl-saved-batch" onclick="downloadSavedBatch(this)" style="display:none;">⬇ Download Top 20 Liked Songs</button>
          <button class="btn-sm" id="btn-song-prev" onclick="changeSongPage(-1)" disabled>◀ Previous</button>
          <span id="song-page-indicator" style="font-size:11px; color:var(--muted); font-variant-numeric:tabular-nums;">Page 1 of 1</span>
          <button class="btn-sm" id="btn-song-next" onclick="changeSongPage(1)" disabled>Next ▶</button>
        </div>
      </div>
      <div style="max-height: 480px; overflow-y: auto; border: 1px solid var(--line); border-radius: 6px;">
        <table id="songs-table">
          <thead>
            <tr>
              <th style="width: 44px;">#</th>
              <th>Song Title</th>
              <th>Artist</th>
              <th>Album</th>
              <th style="text-align:right;">Length</th>
              <th>FiiO Card State</th>
              <th style="width: 110px;">Action</th>
            </tr>
          </thead>
          <tbody id="songs-body">
            <tr><td colspan="7" class="empty-msg">Loading songs…</td></tr>
          </tbody>
        </table>
      </div>
    </div>

    <!-- Downloaded Local View -->
    <div id="tab-view-downloaded" style="display:none;">
      <div style="margin-bottom: 10px; display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:8px;">
        <span id="downloaded-summary-label" style="color:var(--muted); font-size:11px;">Showing locally downloaded and verified FLAC files on disk</span>
        <div style="display:flex; align-items:center; gap:8px;">
          <button class="btn-sm" onclick="exportAllM3uPlaylists(this)">📑 Re-export M3U8 Playlists</button>
          <button class="btn-sm primary" onclick="triggerSync(this, false)">⚡ Sync All to Card</button>
        </div>
      </div>
      <div style="max-height: 480px; overflow-y: auto; border: 1px solid var(--line); border-radius: 6px;">
        <table id="downloaded-table">
          <thead>
            <tr>
              <th style="width: 44px;">#</th>
              <th>Song Title</th>
              <th>Artist</th>
              <th>Album</th>
              <th>Format</th>
              <th style="text-align:right;">Size</th>
              <th>Storage Location</th>
              <th>FiiO MicroSD State</th>
            </tr>
          </thead>
          <tbody id="downloaded-body">
            <tr><td colspan="8" class="empty-msg">Loading downloaded files…</td></tr>
          </tbody>
        </table>
      </div>
    </div>
  </section>

  <!-- Candidates with Multi-Select and Bulk Actions -->
  <section class="full-width">
    <div class="sec-head">
      <div>
        <h2>Discovery Candidates (Awaiting Review)</h2>
        <span class="badge warn" id="cand-count">0 pending</span>
        <span id="selection-counter" style="margin-left: 8px; font-size: 11px; color: var(--accent); font-weight: 500;"></span>
      </div>
      <div class="sec-head-actions">
        <button class="btn-sm success" id="btn-approve-selected" onclick="approveSelected()" disabled>✓ Approve Selected</button>
        <button class="btn-sm danger" id="btn-reject-selected" onclick="rejectSelected()" disabled>✕ Reject Selected</button>
        <button class="btn-sm primary" onclick="approveAllPending()">✓ Approve All Pending</button>
      </div>
    </div>
    <div style="overflow-x:auto;">
      <table id="candidates-table">
        <thead>
          <tr>
            <th style="width: 32px;"><input type="checkbox" id="select-all-cands" onchange="toggleSelectAll(this)"></th>
            <th style="width: 50px;">#</th>
            <th>Artist</th>
            <th>Release</th>
            <th>Score</th>
            <th>Licence Rights</th>
            <th>Reason</th>
            <th style="width: 140px;">Action</th>
          </tr>
        </thead>
        <tbody id="candidates-body">
          <tr><td colspan="8" class="empty-msg">No candidates awaiting review</td></tr>
        </tbody>
      </table>
    </div>
  </section>

  <!-- Sync History -->
  <section>
    <div class="sec-head"><h2>Recent Sync Runs</h2></div>
    <table id="sync-table">
      <thead><tr><th>#</th><th>Date</th><th>Status</th><th>Added</th><th>Skipped</th></tr></thead>
      <tbody id="sync-body"></tbody>
    </table>
  </section>

  <!-- Automated Jobs -->
  <section>
    <div class="sec-head"><h2>Scheduled Tasks</h2></div>
    <table id="auto-table">
      <thead><tr><th>Task</th><th>Schedule</th><th>Last Executed</th><th>Due</th></tr></thead>
      <tbody id="auto-body"></tbody>
    </table>
  </section>
</main>

<!-- Playlist Detail Modal -->
<div id="playlist-modal" class="modal-overlay" onclick="onModalOverlayClick(event)">
  <div class="modal-dialog">
    <div style="padding:16px 20px; border-bottom:1px solid var(--line); display:flex; justify-content:space-between; align-items:center;">
      <div>
        <h3 id="modal-pl-title" style="margin:0; font-size:15px; font-weight:600; color:#fff;">Playlist Title</h3>
        <span id="modal-pl-subtitle" style="font-size:11px; color:var(--muted);">Curator • Total Tracks</span>
      </div>
      <div style="display:flex; align-items:center; gap:10px;">
        <button class="btn-sm primary" id="btn-modal-dl-all" onclick="downloadCurrentPlaylist(this)">⬇ Download Playlist</button>
        <button class="btn-sm" onclick="closePlaylistModal()">✕ Close</button>
      </div>
    </div>
    <div style="padding:14px 20px; overflow-y:auto; flex:1;">
      <table id="modal-pl-table">
        <thead>
          <tr>
            <th style="width:36px;">#</th>
            <th>Song Title</th>
            <th>Artist</th>
            <th>Album</th>
            <th style="text-align:right;">Length</th>
            <th>FiiO Echo Mini State</th>
            <th style="width:110px;">Action</th>
          </tr>
        </thead>
        <tbody id="modal-pl-body">
          <tr><td colspan="7" class="empty-msg">Loading tracks…</td></tr>
        </tbody>
      </table>
    </div>
  </div>
</div>

<footer>
  <span>Local loopback only (127.0.0.1). Hardware limits strictly checked against Echo Mini.</span>
  <span>Auto-refreshes every 10 seconds</span>
</footer>

<script>
const fmt = n => n == null ? "-" : new Intl.NumberFormat().format(n);
const gb = n => (n == null || isNaN(n)) ? "0.00 GB" : (n/1024/1024/1024).toFixed(2) + " GB";
const msToMin = ms => ms ? (Math.floor(ms/60000) + ":" + String(Math.floor((ms%60000)/1000)).padStart(2,"0")) : "-";
const kv = (label, value, cls="") => `<div class="kv"><span>${label}</span><b class="${cls}">${value}</b></div>`;
const escapeHtml = str => String(str || "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");

let allCurrentCandidates = [];
let selectedCandidateIds = new Set();
let searchDebounceTimer = null;

let currentBrowserTab = "playlists";
let playlistsData = [];
let songFilterQuery = "";
let currentSongPage = 1;
let totalSongPages = 1;
let browserFilterTimer = null;
let initialPlaylistsLoaded = false;

function showToast(msg, isError=false) {
  const el = document.getElementById("toast");
  el.style.display = "block";
  el.style.borderColor = isError ? "var(--bad)" : "var(--accent)";
  el.innerHTML = msg;
  setTimeout(() => { el.style.display = "none"; }, 7000);
}

async function safeFetch(url) {
  try {
    const res = await fetch(url);
    if (!res.ok) return { error: `HTTP ${res.status}` };
    return await res.json();
  } catch (err) {
    return { error: err.message };
  }
}

async function refreshAll() {
  document.getElementById("stamp").textContent = "refreshing…";

  const [s, lib, dev, syn, cands, auto] = await Promise.all([
    safeFetch("/api/status"),
    safeFetch("/api/library"),
    safeFetch("/api/devices"),
    safeFetch("/api/sync"),
    safeFetch("/api/candidates?status=awaiting_approval"),
    safeFetch("/api/automation")
  ]);

  // Library
  if (lib && !lib.error) {
    document.getElementById("library").innerHTML =
      kv("Total Files", fmt(lib.files)) +
      kv("Verified FLAC", fmt(lib.verified_flac), "ok") +
      kv("Albums / Artists", fmt(lib.albums) + " / " + fmt(lib.artists)) +
      kv("Disk Usage", gb(lib.size_bytes)) +
      kv("Transcode Suspects", fmt(lib.transcode_suspect), lib.transcode_suspect ? "warn" : "");
  }

  // Spotify
  if (s && !s.error && s.spotify) {
    const sp = s.spotify;
    const sc = sp.counts || {};
    document.getElementById("spotify").innerHTML =
      kv("Account Connected", sp.logged_in ? "Connected (PKCE)" : "Not Connected", sp.logged_in ? "ok" : "bad") +
      kv("Saved Tracks", fmt(sc.saved_tracks)) +
      kv("Playlists Indexed", fmt(sc.playlists)) +
      kv("Taste Artists", fmt(sp.profile_artists)) +
      kv("Last Spotify Sync", sp.last_sync || "never");
  }

  // Devices
  if (dev && !dev.error) {
    const volumes = dev.volumes || [];
    const b = document.getElementById("card-badge");
    if (volumes.length) {
      const v = volumes[0];
      b.textContent = v.confirmed ? "confirmed" : "unconfirmed";
      b.className = "badge " + (v.confirmed ? "ok" : "warn");
      document.getElementById("devices").innerHTML =
        kv("Volume Name", v.volume_name) +
        kv("Filesystem", v.filesystem + " (Echo Mini ready)") +
        kv("Capacity", v.capacity_gb + " GB") +
        kv("Mount Point", v.mount_point) +
        kv("Target State", v.confirmed ? "Ready for Sync" : "Needs Confirmation", v.confirmed ? "ok" : "warn");
    } else {
      b.textContent = "offline";
      b.className = "badge bad";
      document.getElementById("devices").innerHTML = kv("Card", "No microSD card detected", "warn");
    }
  }

  // Pipeline & Tabs
  if (s && !s.error && s.counts) {
    const c = s.counts;
    document.getElementById("pipeline").innerHTML =
      kv("Total Candidates", fmt(c.candidates)) +
      kv("Awaiting Review", fmt(c.awaiting_approval), c.awaiting_approval ? "warn" : "") +
      kv("Approved Acquisitions", fmt(c.acquisitions)) +
      kv("Total Sync Runs", fmt(c.sync_runs)) +
      kv("Reserved Capacity", "1.50 GB (Protected)");

    if (c.playlists != null) document.getElementById("pl-tab-count").textContent = fmt(c.playlists);
    if (c.saved_tracks != null) document.getElementById("saved-tab-count").textContent = fmt(c.saved_tracks);
    if (c.tracks != null) document.getElementById("all-songs-tab-count").textContent = fmt(c.tracks);
    if (c.downloaded_files != null) document.getElementById("downloaded-tab-count").textContent = fmt(c.downloaded_files);
  }

  if (!initialPlaylistsLoaded) {
    initialPlaylistsLoaded = true;
    loadPlaylists();
  }

  // Candidates Table with Checkboxes
  allCurrentCandidates = (cands && !cands.error) ? (cands.candidates || []) : [];
  document.getElementById("cand-count").textContent = allCurrentCandidates.length + " pending";
  renderCandidatesTable();

  // Sync Runs
  if (syn && !syn.error) {
    const runs = syn.runs || [];
    const syncBody = document.getElementById("sync-body");
    if (!runs.length) {
      syncBody.innerHTML = `<tr><td colspan="5" class="empty-msg">No sync runs recorded</td></tr>`;
    } else {
      syncBody.innerHTML = runs.slice(0, 6).map(r => `
        <tr>
          <td>#${r.id}</td>
          <td>${(r.started_at || "").slice(0, 16).replace("T", " ")}</td>
          <td><span class="badge ${r.status==='completed'?'ok':'warn'}">${r.status}</span></td>
          <td><b>${fmt(r.added)}</b></td>
          <td>${fmt(r.skipped)}</td>
        </tr>
      `).join("");
    }
  }

  // Automation
  if (auto && !auto.error) {
    const jobs = auto.jobs || [];
    document.getElementById("auto-body").innerHTML = jobs.map(j => `
      <tr>
        <td><b>${j.name}</b></td>
        <td>${j.schedule}</td>
        <td>${j.last_run ? j.last_run.slice(0, 16).replace("T", " ") : "never"}</td>
        <td><span class="badge ${j.due ? "warn" : "ok"}">${j.due ? "due" : "idle"}</span></td>
      </tr>
    `).join("");
  }

  document.getElementById("stamp").textContent = "updated " + new Date().toLocaleTimeString();
}

let selectedPlaylistIds = new Set();
let downloadedFilesData = [];

async function switchBrowserTab(tab) {
  currentBrowserTab = tab;
  document.querySelectorAll("#browser-tabs .tab-btn").forEach(b => b.classList.remove("active"));
  const activeBtn = document.getElementById(`tab-btn-${tab}`);
  if (activeBtn) activeBtn.classList.add("active");

  const plView = document.getElementById("tab-view-playlists");
  const songsView = document.getElementById("tab-view-songs");
  const dlView = document.getElementById("tab-view-downloaded");
  const filterInput = document.getElementById("browser-filter-input");
  const btnDlSaved = document.getElementById("btn-dl-saved-batch");
  filterInput.value = "";
  songFilterQuery = "";

  if (tab === "playlists") {
    plView.style.display = "block";
    songsView.style.display = "none";
    if (dlView) dlView.style.display = "none";
    if (btnDlSaved) btnDlSaved.style.display = "none";
    filterInput.placeholder = "Filter playlists by name or curator…";
    loadPlaylists();
  } else if (tab === "saved") {
    plView.style.display = "none";
    songsView.style.display = "block";
    if (dlView) dlView.style.display = "none";
    if (btnDlSaved) btnDlSaved.style.display = "inline-flex";
    filterInput.placeholder = "Filter liked songs…";
    document.getElementById("songs-count-label").textContent = "Showing Spotify Liked Songs";
    currentSongPage = 1;
    loadSongs();
  } else if (tab === "all_songs") {
    plView.style.display = "none";
    songsView.style.display = "block";
    if (dlView) dlView.style.display = "none";
    if (btnDlSaved) btnDlSaved.style.display = "none";
    filterInput.placeholder = "Filter all songs by title, artist, or album…";
    document.getElementById("songs-count-label").textContent = "Showing All Library Songs";
    currentSongPage = 1;
    loadSongs();
  } else if (tab === "downloaded") {
    plView.style.display = "none";
    songsView.style.display = "none";
    if (dlView) dlView.style.display = "block";
    if (btnDlSaved) btnDlSaved.style.display = "none";
    filterInput.placeholder = "Filter downloaded songs by title, artist, or album…";
    loadDownloadedFiles();
  }
}

async function loadPlaylists() {
  const onlyIndexed = document.getElementById("chk-only-indexed").checked;
  const onlyDownloaded = document.getElementById("chk-only-downloaded-pl") ? document.getElementById("chk-only-downloaded-pl").checked : false;
  const q = document.getElementById("browser-filter-input").value.trim();
  const tbody = document.getElementById("playlists-body");
  const data = await safeFetch(`/api/playlists?only_with_tracks=${onlyIndexed ? "true" : "false"}&only_downloaded=${onlyDownloaded ? "true" : "false"}&q=${encodeURIComponent(q)}`);
  if (!data || data.error) {
    tbody.innerHTML = `<tr><td colspan="8" class="empty-msg">Error loading playlists: ${data ? data.error : "network"}</td></tr>`;
    return;
  }
  playlistsData = data.playlists || [];
  if (!q && !onlyIndexed && !onlyDownloaded) {
    document.getElementById("pl-tab-count").textContent = fmt(playlistsData.length);
  }
  renderPlaylistsRows();
}

function renderPlaylistsRows() {
  const tbody = document.getElementById("playlists-body");
  if (!playlistsData.length) {
    tbody.innerHTML = `<tr><td colspan="8" class="empty-msg">No playlists match current filter</td></tr>`;
    updatePlaylistSelectionUI();
    return;
  }
  tbody.innerHTML = playlistsData.map(p => {
    const isChecked = selectedPlaylistIds.has(p.id);
    const canSelect = p.indexed_tracks > 0;
    let statusBadge = "";
    if (p.indexed_tracks > 0) {
      if (p.downloaded_tracks >= p.indexed_tracks) {
        statusBadge = '<span class="badge ok">✓ Full Local</span>';
      } else if (p.downloaded_tracks > 0) {
        statusBadge = `<span class="badge sync">${p.downloaded_tracks}/${p.indexed_tracks} Local</span>`;
      } else {
        statusBadge = '<span class="badge warn">Cloud Only</span>';
      }
    } else {
      statusBadge = '<span class="badge" style="color:var(--muted);">Empty</span>';
    }

    return `
      <tr class="clickable-row" onclick="openPlaylist(${p.id})">
        <td onclick="event.stopPropagation()">
          <input type="checkbox" data-pl-id="${p.id}" ${isChecked ? "checked" : ""} ${canSelect ? "" : "disabled"} onchange="onPlaylistSelectChange(${p.id}, this.checked)">
        </td>
        <td><b>${escapeHtml(p.name)}</b>${p.description ? `<br><span style="color:var(--muted); font-size:11px;">${escapeHtml(p.description.slice(0, 75))}</span>` : ""}</td>
        <td>${escapeHtml(p.owner_name)}</td>
        <td style="text-align:right;">${p.downloaded_tracks > 0 ? `<b class="ok">${fmt(p.downloaded_tracks)}</b>` : `<span style="color:var(--muted); font-size:11px;">0</span>`}</td>
        <td style="text-align:right;">${p.indexed_tracks > 0 ? `<b>${fmt(p.indexed_tracks)}</b>` : `<span style="color:var(--muted); font-size:11px;">0</span>`}</td>
        <td style="text-align:right;">${fmt(p.total_tracks)}</td>
        <td>${statusBadge}</td>
        <td>
          <button class="btn-sm" onclick="event.stopPropagation(); openPlaylist(${p.id})">Inspect (${p.indexed_tracks}) ▶</button>
        </td>
      </tr>
    `;
  }).join("");
  updatePlaylistSelectionUI();
}

function onPlaylistSelectChange(id, checked) {
  if (checked) {
    selectedPlaylistIds.add(id);
  } else {
    selectedPlaylistIds.delete(id);
  }
  updatePlaylistSelectionUI();
}

function toggleSelectAllPlaylists(master) {
  if (master.checked) {
    playlistsData.forEach(p => {
      if (p.indexed_tracks > 0) selectedPlaylistIds.add(p.id);
    });
  } else {
    selectedPlaylistIds.clear();
  }
  renderPlaylistsRows();
}

function updatePlaylistSelectionUI() {
  const count = selectedPlaylistIds.size;
  const counter = document.getElementById("playlist-selected-counter");
  const btn = document.getElementById("btn-dl-selected-playlists");
  const master = document.getElementById("select-all-playlists");
  if (counter) counter.textContent = count ? `(${count} selected)` : "";
  if (btn) btn.disabled = count === 0;
  const selectable = playlistsData.filter(p => p.indexed_tracks > 0);
  if (master && selectable.length > 0) {
    master.checked = count === selectable.length && count > 0;
    master.indeterminate = count > 0 && count < selectable.length;
  }
}

async function downloadSelectedPlaylists(btn) {
  const pids = Array.from(selectedPlaylistIds);
  if (!pids.length) return;
  if (btn) {
    btn.disabled = true;
    btn.textContent = "⏳ Downloading…";
  }
  showToast(`Downloading ${pids.length} selected playlists in batch…`);
  pollActivity();
  try {
    const res = await fetch(`/api/actions/download-playlists-batch?playlist_ids=${pids.join(",")}&max_tracks_per_playlist=30`, { method: "POST" });
    const data = await res.json();
    if (data.ok) {
      showToast(`Batch completed: ${data.downloaded} new tracks downloaded, ${data.skipped} skipped across ${data.playlists_processed} playlists!`);
      selectedPlaylistIds.clear();
      loadPlaylists();
      refreshAll();
    } else {
      showToast("Batch download error: " + (data.error || "failed"), true);
    }
  } catch (err) {
    showToast("Error: " + err.message, true);
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.textContent = "⬇ Download Selected";
    }
    pollActivity();
  }
}

async function downloadAllPlaylists(btn) {
  if (!confirm("Download all indexed playlists into local FLAC storage & export M3U8s?")) return;
  if (btn) {
    btn.disabled = true;
    btn.textContent = "⏳ Downloading All…";
  }
  showToast("Starting batch download for ALL indexed playlists…");
  pollActivity();
  try {
    const res = await fetch("/api/actions/download-all-playlists?max_tracks_per_playlist=30", { method: "POST" });
    const data = await res.json();
    if (data.ok) {
      showToast(`All playlists complete: ${data.downloaded} downloaded, ${data.skipped} skipped!`);
      loadPlaylists();
      refreshAll();
    } else {
      showToast("Download all error: " + (data.error || "failed"), true);
    }
  } catch (err) {
    showToast("Error: " + err.message, true);
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.textContent = "⬇ Download All Playlists";
    }
    pollActivity();
  }
}

async function exportAllM3uPlaylists(btn) {
  if (btn) {
    btn.disabled = true;
    btn.textContent = "⏳ Exporting…";
  }
  showToast("Exporting M3U8 playlists for FiiO player…");
  try {
    const res = await fetch("/api/actions/export-playlists", { method: "POST" });
    const data = await res.json();
    if (data.ok) {
      showToast(`M3U8 playlists updated: ${data.exported} playlists generated (${data.total_tracks} tracks).`);
    } else {
      showToast("Export error: " + (data.error || "failed"), true);
    }
  } catch (err) {
    showToast("Error: " + err.message, true);
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.textContent = "📑 Export M3U8s";
    }
  }
}

async function loadSongs() {
  const tbody = document.getElementById("songs-body");
  const category = (currentBrowserTab === "saved") ? "saved" : "all";
  const q = songFilterQuery;
  const onlyDl = document.getElementById("chk-song-downloaded-only") ? document.getElementById("chk-song-downloaded-only").checked : false;
  const data = await safeFetch(`/api/songs?category=${category}&only_downloaded=${onlyDl ? "true" : "false"}&q=${encodeURIComponent(q)}&page=${currentSongPage}&page_size=50`);
  if (!data || data.error) {
    tbody.innerHTML = `<tr><td colspan="7" class="empty-msg">Error loading songs: ${data ? data.error : "network"}</td></tr>`;
    return;
  }
  const songs = data.songs || [];
  totalSongPages = data.total_pages || 1;
  document.getElementById("song-page-indicator").textContent = `Page ${data.page} of ${totalSongPages} (${fmt(data.total)} songs)`;
  document.getElementById("btn-song-prev").disabled = data.page <= 1;
  document.getElementById("btn-song-next").disabled = data.page >= totalSongPages;

  if (category === "saved") {
    if (!q && !onlyDl) document.getElementById("saved-tab-count").textContent = fmt(data.total);
    document.getElementById("songs-count-label").textContent = `Showing ${fmt(data.total)} Spotify Liked Songs${onlyDl ? " (Downloaded Only)" : ""}`;
  } else {
    if (!q && !onlyDl) document.getElementById("all-songs-tab-count").textContent = fmt(data.total);
    document.getElementById("songs-count-label").textContent = `Showing ${fmt(data.total)} Library Songs${onlyDl ? " (Downloaded Only)" : ""}`;
  }

  if (!songs.length) {
    tbody.innerHTML = `<tr><td colspan="7" class="empty-msg">No songs found matching query</td></tr>`;
    return;
  }

  const offset = (data.page - 1) * 50;
  tbody.innerHTML = songs.map((s, idx) => `
    <tr>
      <td style="color:var(--muted);">${offset + idx + 1}</td>
      <td><b>${escapeHtml(s.title)}</b></td>
      <td>${escapeHtml(s.artist_name)}</td>
      <td>${escapeHtml(s.album_title)}</td>
      <td style="text-align:right;">${msToMin(s.duration_ms)}</td>
      <td>${s.validated ? '<span class="badge ok">✓ On Disk / Ready</span>' : (s.path ? '<span class="badge sync">Local Matched</span>' : '<span class="badge">Spotify Cloud</span>')}</td>
      <td>
        ${s.validated ? '<span class="badge ok">✓ Ready</span>' : `<button class="btn-sm" onclick="downloadSongRow(${s.id}, this)">⬇ Download</button>`}
      </td>
    </tr>
  `).join("");
}

function onSongDownloadedFilterChange() {
  currentSongPage = 1;
  loadSongs();
}

async function loadDownloadedFiles() {
  const tbody = document.getElementById("downloaded-body");
  const q = document.getElementById("browser-filter-input").value.trim();
  const data = await safeFetch(`/api/downloaded-files?q=${encodeURIComponent(q)}`);
  if (!data || data.error) {
    tbody.innerHTML = `<tr><td colspan="8" class="empty-msg">Error loading downloaded files: ${data ? data.error : "network"}</td></tr>`;
    return;
  }
  downloadedFilesData = data.files || [];
  const label = document.getElementById("downloaded-summary-label");
  if (label) {
    label.innerHTML = `<b>${fmt(data.count)}</b> audio tracks stored locally on disk (<b>${gb(data.total_bytes)}</b>)`;
  }
  const tabBadge = document.getElementById("downloaded-tab-count");
  if (tabBadge && !q) {
    tabBadge.textContent = fmt(data.count);
  }
  if (!downloadedFilesData.length) {
    tbody.innerHTML = `<tr><td colspan="8" class="empty-msg">No downloaded local files match query</td></tr>`;
    return;
  }
  tbody.innerHTML = downloadedFilesData.map((f, idx) => `
    <tr>
      <td style="color:var(--muted);">${idx + 1}</td>
      <td><b>${escapeHtml(f.title)}</b></td>
      <td>${escapeHtml(f.artist_name)}</td>
      <td>${escapeHtml(f.album_title)}</td>
      <td><span class="badge ok">${escapeHtml((f.format || "FLAC").toUpperCase())}</span></td>
      <td style="text-align:right;">${(f.size_bytes / 1024 / 1024).toFixed(1)} MB</td>
      <td style="color:var(--muted); font-size:11px; max-width:280px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;" title="${escapeHtml(f.path)}">${escapeHtml(f.rel_path || f.path)}</td>
      <td><span class="badge ok">✓ Card Ready</span></td>
    </tr>
  `).join("");
}

function changeSongPage(delta) {
  const target = currentSongPage + delta;
  if (target >= 1 && target <= totalSongPages) {
    currentSongPage = target;
    loadSongs();
  }
}

function onBrowserFilter(val) {
  clearTimeout(browserFilterTimer);
  browserFilterTimer = setTimeout(() => {
    if (currentBrowserTab === "playlists") {
      loadPlaylists();
    } else if (currentBrowserTab === "downloaded") {
      loadDownloadedFiles();
    } else {
      songFilterQuery = val.trim();
      currentSongPage = 1;
      loadSongs();
    }
  }, 250);
}

let currentOpenPlaylistId = null;

async function openPlaylist(id) {
  currentOpenPlaylistId = id;
  const modal = document.getElementById("playlist-modal");
  const title = document.getElementById("modal-pl-title");
  const subtitle = document.getElementById("modal-pl-subtitle");
  const tbody = document.getElementById("modal-pl-body");

  modal.style.display = "flex";
  title.textContent = "Loading playlist…";
  subtitle.textContent = "";
  tbody.innerHTML = `<tr><td colspan="7" class="empty-msg">Fetching tracks from database…</td></tr>`;

  const data = await safeFetch(`/api/playlists/${id}/tracks`);
  if (!data || data.error) {
    title.textContent = "Playlist Error";
    tbody.innerHTML = `<tr><td colspan="7" class="empty-msg">${data ? data.error : "Failed to load"}</td></tr>`;
    return;
  }

  const pl = data.playlist;
  title.textContent = pl.name;
  subtitle.textContent = `Curator: ${pl.owner_name} • ${data.count} tracks indexed in local database (${pl.total_tracks} total on Spotify)`;

  const tracks = data.tracks || [];
  if (!tracks.length) {
    tbody.innerHTML = `<tr><td colspan="7" class="empty-msg">No tracks indexed for this playlist yet (Spotify Web API 2026 Developer restriction on non-collaborative third-party playlists).</td></tr>`;
    return;
  }

  tbody.innerHTML = tracks.map(t => `
    <tr>
      <td style="color:var(--muted);">${t.position != null ? (t.position + 1) : "-"}</td>
      <td><b>${t.title}</b></td>
      <td>${t.artist_name}</td>
      <td>${t.album_title}</td>
      <td style="text-align:right;">${msToMin(t.duration_ms)}</td>
      <td>${t.validated ? '<span class="badge ok">✓ On Disk / Ready</span>' : (t.path ? '<span class="badge sync">Local Matched</span>' : '<span class="badge">Spotify Cloud Track</span>')}</td>
      <td>
        ${t.validated ? '<span class="badge ok">✓ Ready</span>' : `<button class="btn-sm" onclick="downloadSongRow(${t.id}, this)">⬇ Download</button>`}
      </td>
    </tr>
  `).join("");
}

async function downloadSongRow(trackId, btn) {
  if (btn) {
    btn.disabled = true;
    btn.textContent = "⏳ Downloading…";
  }
  showToast(`Downloading track #${trackId} via yt-dlp & FFmpeg…`);
  pollActivity();
  try {
    const res = await fetch(`/api/actions/download-track?track_id=${trackId}`, { method: "POST" });
    const data = await res.json();
    if (data.ok) {
      showToast(`Downloaded "${data.title}" by ${data.artist}! Validated and ready to sync.`);
      if (btn) {
        btn.outerHTML = '<span class="badge ok">✓ Ready</span>';
      }
      refreshAll();
    } else {
      showToast("Download failed: " + (data.error || "error"), true);
      if (btn) {
        btn.disabled = false;
        btn.textContent = "⬇ Retry";
      }
    }
  } catch (err) {
    showToast("Error: " + err.message, true);
    if (btn) {
      btn.disabled = false;
      btn.textContent = "⬇ Retry";
    }
  } finally {
    pollActivity();
  }
}

async function downloadCurrentPlaylist(btn) {
  if (!currentOpenPlaylistId) return;
  if (btn) {
    btn.disabled = true;
    btn.textContent = "⏳ Downloading…";
  }
  showToast("Downloading playlist songs in background…");
  pollActivity();
  try {
    const res = await fetch(`/api/actions/download-playlist?playlist_id=${currentOpenPlaylistId}&max_tracks=30`, { method: "POST" });
    const data = await res.json();
    if (data.ok) {
      showToast(`Playlist downloaded: ${data.downloaded} new tracks, ${data.skipped} already present!`);
      openPlaylist(currentOpenPlaylistId);
      refreshAll();
    } else {
      showToast("Playlist download error: " + (data.error || "failed"), true);
    }
  } catch (err) {
    showToast("Error: " + err.message, true);
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.textContent = "⬇ Download Playlist";
    }
    pollActivity();
  }
}

async function downloadSavedBatch(btn) {
  if (btn) {
    btn.disabled = true;
    btn.textContent = "⏳ Downloading…";
  }
  showToast("Downloading your top Liked Songs…");
  pollActivity();
  try {
    const res = await fetch("/api/actions/download-saved?limit=20", { method: "POST" });
    const data = await res.json();
    if (data.ok) {
      showToast(`Liked songs complete: ${data.downloaded} new tracks downloaded, ${data.skipped} already present!`);
      loadSongs();
      refreshAll();
    } else {
      showToast("Download error: " + (data.error || "failed"), true);
    }
  } catch (err) {
    showToast("Error: " + err.message, true);
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.textContent = "⬇ Download Top 20 Liked Songs";
    }
    pollActivity();
  }
}

let lastActivityId = 0;
let isAutoScrollEnabled = true;

function toggleAutoScroll() {
  isAutoScrollEnabled = !isAutoScrollEnabled;
  const btn = document.getElementById("btn-autoscroll");
  if (btn) btn.textContent = isAutoScrollEnabled ? "Auto-scroll: ON" : "Auto-scroll: OFF";
  if (isAutoScrollEnabled) {
    const consoleEl = document.getElementById("activity-console");
    if (consoleEl) consoleEl.scrollTop = consoleEl.scrollHeight;
  }
}

async function clearActivityLogs() {
  await safeFetch("/api/actions/activity-clear", { method: "POST" });
  const consoleEl = document.getElementById("activity-console");
  if (consoleEl) consoleEl.innerHTML = '<div class="empty-msg">Console cleared. Waiting for new activity events…</div>';
  lastActivityId = 0;
}

async function clearActivityFailures() {
  await safeFetch("/api/actions/activity-clear-failures", { method: "POST" });
  const panel = document.getElementById("activity-failures-panel");
  if (panel) panel.style.display = "none";
}

async function retryFailedTrack(trackId, btn) {
  if (btn) {
    btn.disabled = true;
    btn.textContent = "⏳ Retrying…";
  }
  showToast(`Retrying download for track #${trackId}…`);
  try {
    const res = await fetch(`/api/actions/download-track?track_id=${trackId}`, { method: "POST" });
    const data = await res.json();
    if (data.ok) {
      showToast(`Downloaded "${data.title}" successfully!`);
      pollActivity();
      refreshAll();
    } else {
      showToast("Retry failed: " + (data.error || "error"), true);
      pollActivity();
      if (btn) {
        btn.disabled = false;
        btn.textContent = "⬇ Retry";
      }
    }
  } catch (err) {
    showToast("Error: " + err.message, true);
    pollActivity();
    if (btn) {
      btn.disabled = false;
      btn.textContent = "⬇ Retry";
    }
  }
}

async function pollActivity() {
  const data = await safeFetch(`/api/activity?since_id=${lastActivityId}`);
  if (!data || data.error) return;

  const badge = document.getElementById("activity-status-badge");
  const desc = document.getElementById("activity-task-desc");
  const progressWrap = document.getElementById("activity-progress-wrap");
  const progressLabel = document.getElementById("activity-progress-label");
  const progressRatio = document.getElementById("activity-progress-ratio");
  const progressBar = document.getElementById("activity-progress-bar");
  const failuresPanel = document.getElementById("activity-failures-panel");
  const failuresCount = document.getElementById("failures-count-label");
  const failuresList = document.getElementById("failures-list");
  const consoleEl = document.getElementById("activity-console");

  if (!badge || !consoleEl) return;

  // 1. Update Active Operation & Progress
  const active = data.active_operation || {};
  if (active.status === "running") {
    badge.className = "badge sync";
    badge.innerHTML = '<span class="pulsing-dot active"></span>Working';
    desc.textContent = active.description || "Active operation in progress…";
    progressWrap.style.display = "block";
    progressLabel.textContent = (active.current_item ? `Working on: ${active.current_item}` : (active.description || "Processing"));
    if (active.progress_total > 0) {
      progressRatio.textContent = `${active.progress_current} / ${active.progress_total}`;
      const pct = Math.min(100, Math.round((active.progress_current / active.progress_total) * 100));
      progressBar.style.width = pct + "%";
    } else {
      progressRatio.textContent = "";
      progressBar.style.width = "100%";
    }
  } else {
    badge.className = "badge ok";
    badge.innerHTML = '<span class="pulsing-dot ready"></span>Idle';
    desc.textContent = active.description || "System idle";
    progressWrap.style.display = "none";
  }

  // 2. Update Failed Tracks Banner
  const failures = data.failed_tracks || [];
  if (failures.length > 0) {
    failuresPanel.style.display = "block";
    failuresCount.textContent = `${failures.length} Failed Track${failures.length > 1 ? "s" : ""}`;
    failuresList.innerHTML = failures.map(f => `
      <div class="failed-track-card">
        <div>
          <b style="color:#fff;">${escapeHtml(f.title)}</b> <span style="color:var(--muted);">by ${escapeHtml(f.artist)}</span>
          <div style="color:var(--bad); font-size:11px; margin-top:2px;"><b>Error:</b> ${escapeHtml(f.error)}</div>
        </div>
        <div style="display:flex; align-items:center; gap:8px;">
          <span style="color:var(--muted); font-size:10px; font-variant-numeric:tabular-nums;">${f.timestamp || ""}</span>
          ${f.track_id ? `<button class="btn-sm primary" onclick="retryFailedTrack(${f.track_id}, this)">⬇ Retry Download</button>` : ""}
        </div>
      </div>
    `).join("");
  } else {
    failuresPanel.style.display = "none";
  }

  // 3. Append New Logs to Console
  const logs = data.logs || [];
  if (logs.length > 0) {
    if (lastActivityId === 0 && consoleEl.querySelector(".empty-msg")) {
      consoleEl.innerHTML = "";
    }
    const htmlChunks = logs.map(e => `
      <div class="log-row">
        <span class="log-time">[${e.timestamp}]</span>
        <span class="log-cat">[${(e.category || "SYSTEM").toUpperCase()}]</span>
        <span class="log-badge ${e.level}">${e.level.toUpperCase()}</span>
        <span class="log-msg ${e.level}">${escapeHtml(e.message)}</span>
      </div>
    `).join("");
    consoleEl.insertAdjacentHTML("beforeend", htmlChunks);
    lastActivityId = data.last_id || lastActivityId;

    if (isAutoScrollEnabled) {
      consoleEl.scrollTop = consoleEl.scrollHeight;
    }
  }
}

function closePlaylistModal() {
  document.getElementById("playlist-modal").style.display = "none";
}

function onModalOverlayClick(e) {
  if (e.target.id === "playlist-modal") closePlaylistModal();
}

document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") closePlaylistModal();
});

function renderCandidatesTable() {
  const candBody = document.getElementById("candidates-body");
  if (!allCurrentCandidates.length) {
    candBody.innerHTML = `<tr><td colspan="8" class="empty-msg">No candidates awaiting review</td></tr>`;
    updateSelectionUI();
    return;
  }
  candBody.innerHTML = allCurrentCandidates.map(c => `
    <tr>
      <td><input type="checkbox" data-id="${c.id}" ${selectedCandidateIds.has(c.id) ? "checked" : ""} onchange="onRowSelectChange(${c.id}, this.checked)"></td>
      <td><b>#${c.id}</b></td>
      <td>${c.artist_name || "-"}</td>
      <td><b>${c.release_title || c.entity_name || "-"}</b></td>
      <td>${c.score ? c.score.toFixed(1) : "-"}</td>
      <td><span class="badge ${c.licence ? "ok" : "warn"}">${c.licence || "unverified"}</span></td>
      <td style="color:var(--muted); font-size:11px;">${(c.reasons || []).map(r => r.code || r.reason || r.text).join(", ")}</td>
      <td>
        <button class="btn-sm success" onclick="approveCandidate(${c.id})">Approve</button>
        <button class="btn-sm danger" onclick="rejectCandidate(${c.id})">Reject</button>
      </td>
    </tr>
  `).join("");
  updateSelectionUI();
}

function onRowSelectChange(id, checked) {
  if (checked) {
    selectedCandidateIds.add(id);
  } else {
    selectedCandidateIds.delete(id);
  }
  updateSelectionUI();
}

function toggleSelectAll(masterCheckbox) {
  if (masterCheckbox.checked) {
    allCurrentCandidates.forEach(c => selectedCandidateIds.add(c.id));
  } else {
    selectedCandidateIds.clear();
  }
  renderCandidatesTable();
}

function updateSelectionUI() {
  const count = selectedCandidateIds.size;
  const countEl = document.getElementById("selection-counter");
  const btnApprove = document.getElementById("btn-approve-selected");
  const btnReject = document.getElementById("btn-reject-selected");
  const masterBox = document.getElementById("select-all-cands");

  countEl.textContent = count ? `(${count} selected)` : "";
  btnApprove.disabled = count === 0;
  btnReject.disabled = count === 0;
  if (masterBox && allCurrentCandidates.length > 0) {
    masterBox.checked = count === allCurrentCandidates.length;
    masterBox.indeterminate = count > 0 && count < allCurrentCandidates.length;
  }
}

async function approveSelected() {
  const ids = Array.from(selectedCandidateIds);
  if (!ids.length) return;
  showToast(`Approving ${ids.length} selected candidates…`);
  try {
    const res = await fetch(`/api/actions/bulk-approve?ids=${ids.join(",")}`, { method: "POST" });
    const data = await res.json();
    if (data.ok) {
      showToast(`Successfully approved ${data.count} candidates!`);
      selectedCandidateIds.clear();
    } else {
      showToast("Bulk approve error: " + (data.error || "failed"), true);
    }
  } catch (err) {
    showToast("Error: " + err.message, true);
  } finally {
    refreshAll();
  }
}

async function rejectSelected() {
  const ids = Array.from(selectedCandidateIds);
  if (!ids.length) return;
  showToast(`Rejecting ${ids.length} selected candidates…`);
  try {
    const res = await fetch(`/api/actions/bulk-reject?ids=${ids.join(",")}`, { method: "POST" });
    const data = await res.json();
    if (data.ok) {
      showToast(`Rejected ${data.count} candidates.`);
      selectedCandidateIds.clear();
    } else {
      showToast("Bulk reject error: " + (data.error || "failed"), true);
    }
  } catch (err) {
    showToast("Error: " + err.message, true);
  } finally {
    refreshAll();
  }
}

async function approveAllPending() {
  if (!confirm(`Are you sure you want to approve all ${allCurrentCandidates.length} pending candidates?`)) return;
  showToast("Approving all pending candidates…");
  try {
    const res = await fetch("/api/actions/approve-all", { method: "POST" });
    const data = await res.json();
    if (data.ok) {
      showToast(`Approved all ${data.count} candidates!`);
      selectedCandidateIds.clear();
    } else {
      showToast("Failed to approve all: " + (data.error || "unknown"), true);
    }
  } catch (err) {
    showToast("Error: " + err.message, true);
  } finally {
    refreshAll();
  }
}

function onSearchInput(query) {
  clearTimeout(searchDebounceTimer);
  if (!query || query.trim().length < 2) {
    closeSearch();
    return;
  }
  searchDebounceTimer = setTimeout(() => { executeSearch(query.trim()); }, 200);
}

async function executeSearch(q) {
  const panel = document.getElementById("search-results-panel");
  const content = document.getElementById("search-results-content");
  const title = document.getElementById("search-results-title");
  panel.style.display = "block";
  title.textContent = `Search Results for "${q}"`;
  content.innerHTML = `<span style="color:var(--muted); font-size:12px;">Searching Spotify library and candidates…</span>`;

  const data = await safeFetch(`/api/search?q=${encodeURIComponent(q)}`);
  if (!data || data.error) {
    content.innerHTML = `<span style="color:var(--bad);">Search error: ${data ? data.error : "network"}</span>`;
    return;
  }

  let html = "";

  // Matching Playlists
  if (data.playlists && data.playlists.length) {
    html += `<h3 style="font-size:12px; text-transform:uppercase; color:#fff; margin:10px 0 6px;">Matching Spotify Playlists (${data.playlists.length})</h3>`;
    html += `<table><thead><tr><th>Playlist Name</th><th>Curator</th><th style="text-align:right;">Indexed Tracks</th><th style="text-align:right;">Total Tracks</th><th>Action</th></tr></thead><tbody>`;
    html += data.playlists.map(p => `
      <tr class="clickable-row" onclick="openPlaylist(${p.id})">
        <td><b>${p.name}</b></td>
        <td>${p.owner_name}</td>
        <td style="text-align:right;">${p.indexed_tracks > 0 ? `<b class="ok">${p.indexed_tracks}</b>` : `<span style="color:var(--muted);">-</span>`}</td>
        <td style="text-align:right;">${p.total_tracks}</td>
        <td>
          <button class="btn-sm" onclick="event.stopPropagation(); openPlaylist(${p.id})">Inspect Songs (${p.indexed_tracks}) ▶</button>
        </td>
      </tr>
    `).join("");
    html += `</tbody></table>`;
  }

  // Matching Library Tracks
  if (data.tracks && data.tracks.length) {
    html += `<h3 style="font-size:12px; text-transform:uppercase; color:var(--accent); margin:16px 0 6px;">Songs & Library Tracks (${data.tracks.length})</h3>`;
    html += `<table><thead><tr><th>Song Title</th><th>Artist</th><th>Album</th><th>Length</th><th>State</th></tr></thead><tbody>`;
    html += data.tracks.map(t => `
      <tr>
        <td><b>${t.title}</b></td>
        <td>${t.artist_name}</td>
        <td>${t.album_title}</td>
        <td>${msToMin(t.duration_ms)}</td>
        <td>${t.validated ? '<span class="badge ok">✓ On Player / Synced</span>' : '<span class="badge sync">Spotify Indexed</span>'}</td>
      </tr>
    `).join("");
    html += `</tbody></table>`;
  }

  // Matching Artists
  if (data.artists && data.artists.length) {
    html += `<h3 style="font-size:12px; text-transform:uppercase; color:var(--good); margin:16px 0 6px;">Artists (${data.artists.length})</h3>`;
    html += `<table><thead><tr><th>Artist Name</th><th>Indexed Tracks</th></tr></thead><tbody>`;
    html += data.artists.map(a => `
      <tr>
        <td><b>${a.name}</b></td>
        <td>${a.tracks_count} tracks</td>
      </tr>
    `).join("");
    html += `</tbody></table>`;
  }

  // Matching Discovery Candidates
  if (data.candidates && data.candidates.length) {
    html += `<h3 style="font-size:12px; text-transform:uppercase; color:var(--warn); margin:16px 0 6px;">Matching Candidates (${data.candidates.length})</h3>`;
    html += `<table><thead><tr><th>#</th><th>Artist</th><th>Release</th><th>Score</th><th>Status</th><th>Action</th></tr></thead><tbody>`;
    html += data.candidates.map(c => `
      <tr>
        <td>#${c.id}</td>
        <td>${c.artist_name || "-"}</td>
        <td><b>${c.entity_name}</b></td>
        <td>${c.score ? c.score.toFixed(1) : "-"}</td>
        <td><span class="badge ${c.status==='approved'?'ok':'warn'}">${c.status}</span></td>
        <td>
          ${c.status==='awaiting_approval' ? `
            <button class="btn-sm success" onclick="approveCandidate(${c.id})">Approve</button>
            <button class="btn-sm danger" onclick="rejectCandidate(${c.id})">Reject</button>
          ` : `<span style="color:var(--muted); font-size:11px;">Already ${c.status}</span>`}
        </td>
      </tr>
    `).join("");
    html += `</tbody></table>`;
  }

  if (!data.playlists?.length && !data.tracks.length && !data.artists.length && !data.candidates.length) {
    html = `<div class="empty-msg">No playlists, songs, artists, or candidates matched "${q}"</div>`;
  }

  content.innerHTML = html;
}

function closeSearch() {
  document.getElementById("search-results-panel").style.display = "none";
}

async function triggerScan(btn) {
  btn.disabled = true;
  showToast("Scanning library directories for audio files…");
  try {
    const res = await fetch("/api/actions/scan", { method: "POST" });
    const data = await res.json();
    if (data.ok) {
      const s = data.summary;
      showToast(`Scan complete: ${s.scanned} scanned, ${s.indexed} indexed, ${s.matched} matched to Spotify`);
    } else {
      showToast("Scan failed: " + (data.error || "unknown"), true);
    }
  } catch (err) {
    showToast("Scan error: " + err.message, true);
  } finally {
    btn.disabled = false;
    refreshAll();
  }
}

async function triggerSync(btn, dryRun) {
  btn.disabled = true;
  showToast(dryRun ? "Calculating sync plan for microSD…" : "Syncing files to microSD card…");
  pollActivity();
  try {
    const res = await fetch(`/api/actions/sync?dry_run=${dryRun ? "true" : "false"}`, { method: "POST" });
    const data = await res.json();
    if (data.ok) {
      if (dryRun) {
        const p = data.plan;
        showToast(`Sync Plan: ${p.counts.add} to add, ${p.counts.skip} unchanged, Fits: ${p.fits ? "YES" : "NO"} (${gb(p.planned_bytes)})`);
      } else {
        const r = data.result;
        showToast(`Sync Complete! Added: ${r.added}, Verified: ${r.verified}, Skipped: ${r.skipped}`, false);
      }
    } else {
      showToast("Sync failed: " + (data.error || "unknown"), true);
    }
  } catch (err) {
    showToast("Sync error: " + err.message, true);
  } finally {
    btn.disabled = false;
    pollActivity();
    refreshAll();
  }
}

async function triggerSpotifySync(btn) {
  btn.disabled = true;
  showToast("Fetching latest saved tracks and playlists from Spotify…");
  try {
    const res = await fetch("/api/actions/spotify-sync", { method: "POST" });
    const data = await res.json();
    if (data.ok) {
      showToast("Spotify synced successfully!");
    } else {
      showToast("Spotify sync failed: " + (data.error || "unknown"), true);
    }
  } catch (err) {
    showToast("Error: " + err.message, true);
  } finally {
    btn.disabled = false;
    refreshAll();
  }
}

async function approveCandidate(id) {
  showToast(`Approving candidate #${id}…`);
  try {
    const res = await fetch(`/api/actions/approve?candidate_id=${id}`, { method: "POST" });
    const data = await res.json();
    if (data.ok) {
      showToast(`Candidate #${id} approved!`);
      selectedCandidateIds.delete(id);
    } else {
      showToast("Could not approve: " + data.error, true);
    }
  } catch (err) {
    showToast("Error: " + err.message, true);
  } finally {
    refreshAll();
  }
}

async function rejectCandidate(id) {
  showToast(`Rejecting candidate #${id}…`);
  try {
    const res = await fetch(`/api/actions/reject?candidate_id=${id}`, { method: "POST" });
    const data = await res.json();
    if (data.ok) {
      showToast(`Candidate #${id} rejected.`);
      selectedCandidateIds.delete(id);
    } else {
      showToast("Could not reject: " + data.error, true);
    }
  } catch (err) {
    showToast("Error: " + err.message, true);
  } finally {
    refreshAll();
  }
}

refreshAll();
setInterval(refreshAll, 10000);
pollActivity();
setInterval(pollActivity, 1500);
</script>
</body>
</html>
"""
