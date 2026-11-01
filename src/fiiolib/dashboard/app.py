"""Local-only FastAPI dashboard (127.0.0.1, read-only JSON + static page + actions)."""

from __future__ import annotations

import json
import logging
import os
import shutil
from pathlib import Path
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

ALLOWED_HOSTS = {"127.0.0.1", "localhost", "::1", "testclient"}


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
    def candidates(
        status_filter: str | None = Query(default=None, alias="status"),
        limit: int = 200,
        q: str = Query(default=""),
    ) -> dict[str, Any]:
        rows = candidate_rows(db, status=status_filter, limit=min(limit, 500), query=q)
        counts = {
            "all": int(db.scalar("SELECT COUNT(*) FROM discovery_candidates") or 0),
            "awaiting_approval": int(
                db.scalar("SELECT COUNT(*) FROM discovery_candidates WHERE status = 'awaiting_approval'") or 0
            ),
            "approved": int(db.scalar("SELECT COUNT(*) FROM discovery_candidates WHERE status = 'approved'") or 0),
            "rejected": int(db.scalar("SELECT COUNT(*) FROM discovery_candidates WHERE status = 'rejected'") or 0),
        }
        return {"count": len(rows), "candidates": rows, "counts": counts}

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
    def action_download_saved(limit: int = Query(default=0)) -> dict[str, Any]:
        from fiiolib.downloads.downloader import download_saved_tracks
        lim = limit if limit > 0 else None
        return download_saved_tracks(config, db, limit=lim)

    @app.post("/api/actions/download-tracks-batch")
    def action_download_tracks_batch(track_ids: str = Query(...)) -> dict[str, Any]:
        from fiiolib.downloads.downloader import download_tracks_batch
        tids = [int(x.strip()) for x in track_ids.split(",") if x.strip().isdigit()]
        if not tids:
            return {"ok": False, "error": "No valid track IDs provided"}
        return download_tracks_batch(config, db, tids)

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

    @app.post("/api/actions/discover")
    def action_discover(limit_artists: int = Query(default=10)) -> dict[str, Any]:
        from fiiolib.activity import activity
        from fiiolib.discovery.engine import DiscoveryEngine
        activity.start_operation("discovery", "Discovering new music from Spotify, MusicBrainz & Archive", total=limit_artists)
        activity.log(level="info", category="discovery", message="Starting discovery engine scan across authorised sources…")

        auth = SpotifyAuth(config, db)
        spotify = SpotifyClient(config, db, auth=auth) if auth.is_logged_in() else None
        engine = DiscoveryEngine(config, db, spotify=spotify)
        try:
            summary = engine.run(
                limit_artists=limit_artists,
                use_spotify=spotify is not None,
                use_musicbrainz=True,
                use_internet_archive=True,
            )
            data = summary.as_dict()
            total_cand = int(db.scalar("SELECT COUNT(*) FROM discovery_candidates") or 0)
            pending_cand = int(
                db.scalar("SELECT COUNT(*) FROM discovery_candidates WHERE status = 'awaiting_approval'") or 0
            )
            activity.finish_operation(
                f"Discovery complete: {data.get('albums', 0)} new candidate albums found ({pending_cand} awaiting review)"
            )
            activity.log(
                level="success",
                category="discovery",
                message=f"✓ Discovery complete: found {data.get('albums', 0)} unowned releases. Pending review: {pending_cand}.",
            )
            return {"ok": True, "summary": data, "total_candidates": total_cand, "pending": pending_cand}
        except Exception as exc:
            logger.exception("Discovery failed")
            activity.finish_operation("Discovery scan failed")
            activity.log(level="error", category="discovery", message=f"Discovery scan failed: {exc}", error=str(exc))
            return {"ok": False, "error": str(exc)}
        finally:
            engine.close()
            if spotify:
                spotify.close()

    @app.get("/api/discovery/explore")
    def discovery_explore(q: str = Query(default="")) -> dict[str, Any]:
        """Search Spotify or MusicBrainz for new discoverable music with track details."""
        if not q or len(q.strip()) < 2:
            return {"query": "", "results": []}
        auth = SpotifyAuth(config, db)
        results: list[dict[str, Any]] = []
        if auth.is_logged_in():
            client = SpotifyClient(config, db, auth=auth)
            try:
                res = client.search(q.strip(), types=("track", "album"), limit=10)
                tracks = res.get("tracks", {}).get("items") or []
                for t in tracks:
                    artists = t.get("artists") or []
                    artist_name = artists[0].get("name") if artists else "Unknown"
                    album = t.get("album") or {}
                    album_title = album.get("name") or "Single"
                    cover = (album.get("images") or [{}])[0].get("url") if album.get("images") else None
                    in_db = db.one(
                        "SELECT t.id, f.validated FROM tracks t LEFT JOIN files f ON f.track_id = t.id WHERE t.title = ? LIMIT 1",
                        (t.get("name"),),
                    )
                    results.append(
                        {
                            "spotify_id": t.get("id"),
                            "title": t.get("name"),
                            "artist_name": artist_name,
                            "album_title": album_title,
                            "duration_ms": t.get("duration_ms"),
                            "cover_url": cover,
                            "release_date": album.get("release_date"),
                            "in_library": bool(in_db),
                            "is_ready": bool(in_db and in_db["validated"]),
                            "db_track_id": in_db["id"] if in_db else None,
                        }
                    )
            except Exception as exc:
                logger.warning("Discovery explore search failed: %s", exc)
            finally:
                client.close()
        return {"query": q, "results": results}

    @app.post("/api/actions/download-candidate")
    def action_download_candidate(candidate_id: int = Query(...)) -> dict[str, Any]:
        """Download candidate release directly to FLAC."""
        cand = db.one(
            "SELECT c.*, a.name AS artist_name FROM discovery_candidates c "
            "LEFT JOIN artists a ON a.id = c.artist_id WHERE c.id = ?",
            (candidate_id,),
        )
        if not cand:
            return {"ok": False, "error": f"Candidate {candidate_id} not found"}

        repo.set_candidate_status(db, candidate_id, "approved")
        title = cand["entity_name"]
        artist = cand["artist_name"] or "Unknown"

        artist_id = cand["artist_id"] or repo.get_or_create_artist(db, artist)
        album_id = cand["album_id"] or repo.get_or_create_album(db, title, artist_id=artist_id)
        track_id = repo.get_or_create_track(db, title, artist_id=artist_id, album_id=album_id)

        from fiiolib.downloads.downloader import download_track, export_all_playlists
        res = download_track(config, db, track_id)
        if res.get("ok"):
            export_all_playlists(config, db)
        return res

    @app.post("/api/actions/download-explore-track")
    def action_download_explore_track(
        title: str = Query(...),
        artist: str = Query(...),
        album: str = Query(default="Single"),
        spotify_id: str | None = Query(default=None),
        cover_url: str | None = Query(default=None),
    ) -> dict[str, Any]:
        """Directly acquire any newly discovered track into library in FLAC."""
        artist_id = repo.get_or_create_artist(db, artist)
        album_id = repo.get_or_create_album(db, album, artist_id=artist_id, cover_url=cover_url)
        track_id = repo.get_or_create_track(db, title, artist_id=artist_id, album_id=album_id, spotify_id=spotify_id)

        from fiiolib.downloads.downloader import download_track, export_all_playlists
        res = download_track(config, db, track_id)
        if res.get("ok"):
            export_all_playlists(config, db)
        return res

    @app.get("/api/system/folders")
    def system_folders(path: str = Query(default="")) -> dict[str, Any]:
        """List directories and storage details for interactive folder selection."""
        if not path or not path.strip():
            target_path = Path.home()
        else:
            target_path = Path(path).expanduser().resolve()

        if not target_path.exists() or not target_path.is_dir():
            target_path = Path.home()

        parent = str(target_path.parent) if target_path.parent != target_path else None

        subdirs: list[dict[str, Any]] = []
        try:
            for item in sorted(target_path.iterdir(), key=lambda p: p.name.lower()):
                if item.is_dir() and not item.name.startswith("."):
                    subdirs.append({"name": item.name, "path": str(item)})
        except (PermissionError, OSError):
            pass

        quick_paths = [
            {"label": "Home (~)", "path": str(Path.home())},
            {"label": "Music", "path": str(Path.home() / "Music")},
            {"label": "Downloads", "path": str(Path.home() / "Downloads")},
            {"label": "Library", "path": str(config.root)},
        ]
        if Path("/Volumes").exists():
            quick_paths.append({"label": "Volumes (/Volumes)", "path": "/Volumes"})
            try:
                for v in sorted(Path("/Volumes").iterdir()):
                    if v.is_dir() and not v.name.startswith("."):
                        quick_paths.append({"label": f"Card: {v.name}", "path": str(v)})
            except (PermissionError, OSError):
                pass

        audio_count = 0
        try:
            for it in target_path.iterdir():
                if it.is_file() and it.suffix.lower() in {".flac", ".mp3", ".m4a", ".wav", ".ogg", ".ape", ".dsf", ".dff", ".wma"}:
                    audio_count += 1
        except (PermissionError, OSError):
            pass

        try:
            usage = shutil.disk_usage(target_path)
            free_gb = round(usage.free / (1024**3), 2)
            total_gb = round(usage.total / (1024**3), 2)
        except OSError:
            free_gb = 0.0
            total_gb = 0.0

        is_writable = os.access(str(target_path), os.W_OK)

        return {
            "current_path": str(target_path),
            "parent_path": parent,
            "subdirectories": subdirs[:120],
            "quick_paths": quick_paths,
            "audio_count": audio_count,
            "free_gb": free_gb,
            "total_gb": total_gb,
            "is_writable": is_writable,
        }

    @app.post("/api/settings/target-device")
    def set_target_device(mount_point: str = Query(...), confirm: bool = Query(default=True)) -> dict[str, Any]:
        """Set a custom sync target folder or SD card mount point."""
        from fiiolib.activity import activity
        path = Path(mount_point).expanduser().resolve()
        if not path.is_dir():
            return {"ok": False, "error": f"Path '{path}' is not an existing directory"}

        config.device.mount_point = str(path)
        try:
            planner = SyncPlanner(config, db)
            vol = DeviceLocator(config).current(str(path))
            device_id = planner.confirm(vol) if confirm else planner.register(vol)
            activity.log(
                level="success",
                category="device",
                message=f"✓ Target sync folder set to: {path} ({vol.capacity_gb} GB)",
            )
            return {"ok": True, "mount_point": str(path), "volume_name": vol.volume_name, "device_id": device_id}
        except Exception as exc:
            logger.exception("Failed to set target device")
            return {"ok": False, "error": str(exc)}

    @app.post("/api/settings/library-root")
    def set_library_root(path: str = Query(...)) -> dict[str, Any]:
        """Update the active library storage directory."""
        from fiiolib.activity import activity
        new_path = Path(path).expanduser().resolve()
        try:
            new_path.mkdir(parents=True, exist_ok=True)
            config.app.library_root = new_path
            config.ensure_directories()
            activity.log(
                level="success",
                category="system",
                message=f"✓ Library root directory updated to: {new_path}",
            )
            return {"ok": True, "library_root": str(config.root)}
        except Exception as exc:
            logger.exception("Failed to update library root")
            return {"ok": False, "error": str(exc)}

    @app.get("/api/folder/scan")
    def api_scan_folder(path: str = Query(...)) -> dict[str, Any]:
        """Preview audio tracks in a folder before importing."""
        from fiiolib.library.importer import scan_folder
        summary = scan_folder(path)
        return {"ok": True, "summary": summary.as_dict()}

    @app.post("/api/actions/import-folder")
    def api_import_folder(folder_path: str = Query(...), copy_files: bool = Query(default=True)) -> dict[str, Any]:
        """Scan and import audio files from a local directory into library."""
        from fiiolib.library.importer import FolderImporter
        importer = FolderImporter(config, db)
        return importer.import_folder(folder_path, copy_files=copy_files)

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
    capabilities["storage"] = {
        "library_root": str(config.root),
        "target_mount_point": getattr(config.device, "mount_point", None),
        "device_music_subdir": config.device.music_subdir,
    }
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
.cand-cover {
  width: 32px;
  height: 32px;
  border-radius: 3px;
  object-fit: cover;
  background: #141c2b;
  display: inline-block;
  vertical-align: middle;
}
.explore-item {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 8px 12px;
  background: #0d131e;
  border: 1px solid var(--line);
  border-radius: 4px;
  gap: 12px;
  font-size: 12px;
}

/* Storage & Folder Modal */
.modal-overlay {
  position: fixed;
  inset: 0;
  background: rgba(0, 0, 0, 0.75);
  backdrop-filter: blur(4px);
  z-index: 1000;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 20px;
}
.modal-box {
  background: #0d131d;
  border: 1px solid var(--line-light);
  border-radius: 8px;
  width: 100%;
  max-width: 680px;
  max-height: 88vh;
  display: flex;
  flex-direction: column;
  box-shadow: 0 20px 40px rgba(0,0,0,0.6);
  overflow: hidden;
}
.modal-header {
  padding: 14px 18px;
  border-bottom: 1px solid var(--line);
  display: flex;
  justify-content: space-between;
  align-items: center;
  background: #111722;
}
.modal-tabs {
  display: flex;
  border-bottom: 1px solid var(--line);
  background: #090d13;
  padding: 0 14px;
  gap: 6px;
}
.modal-tab-btn {
  background: transparent;
  border: none;
  border-bottom: 2px solid transparent;
  border-radius: 0;
  padding: 10px 14px;
  color: var(--muted);
  font-size: 12.5px;
  font-weight: 500;
}
.modal-tab-btn:hover { color: var(--fg); background: transparent; }
.modal-tab-btn.active {
  color: var(--accent);
  border-bottom-color: var(--accent);
}
.modal-body {
  padding: 16px 18px;
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  gap: 14px;
}
.modal-footer {
  padding: 12px 18px;
  border-top: 1px solid var(--line);
  display: flex;
  justify-content: space-between;
  align-items: center;
  background: #111722;
}
.folder-desc { font-size: 12px; color: var(--muted); margin-bottom: 10px; }
.folder-current-box {
  background: #141c2b;
  border: 1px solid var(--line);
  border-radius: 6px;
  padding: 8px 12px;
  margin-bottom: 12px;
}
.folder-label { font-size: 11px; text-transform: uppercase; color: var(--muted); letter-spacing: 0.04em; }
.folder-val { font-size: 13px; font-weight: 600; color: #fff; font-family: ui-monospace, monospace; word-break: break-all; margin-top: 2px; }
.folder-section-title { font-size: 12px; font-weight: 600; color: var(--fg); margin-bottom: 6px; }
.detected-volumes-list { display: flex; flex-direction: column; gap: 6px; }
.volume-item {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 8px 12px;
  background: #161e2c;
  border: 1px solid var(--line);
  border-radius: 6px;
  cursor: pointer;
  transition: all 0.15s ease;
}
.volume-item:hover { border-color: var(--accent); background: #1c2638; }
.volume-item.active { border-color: var(--accent); background: rgba(56, 189, 248, 0.12); }
.folder-picker-bar { display: flex; gap: 8px; }
.folder-picker-bar input {
  flex: 1;
  background: #141c2b;
  border: 1px solid var(--line-light);
  border-radius: 6px;
  padding: 6px 10px;
  color: #fff;
  font-family: ui-monospace, monospace;
  font-size: 12px;
  outline: none;
}
.folder-browser-box {
  background: #0a0e14;
  border: 1px solid var(--line);
  border-radius: 6px;
  padding: 10px 12px;
}
.browser-header { display: flex; flex-direction: column; gap: 6px; margin-bottom: 8px; }
.quick-paths-row { display: flex; flex-wrap: wrap; gap: 6px; }
.quick-chip {
  font-size: 11px;
  background: #1e293b;
  border: 1px solid var(--line);
  border-radius: 4px;
  padding: 3px 8px;
  color: var(--muted);
  cursor: pointer;
}
.quick-chip:hover { color: var(--accent); border-color: var(--accent); background: #243147; }
.browser-current-row {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 6px 0;
  border-top: 1px solid var(--line);
  border-bottom: 1px solid var(--line);
  margin-bottom: 8px;
}
.browser-path-display {
  flex: 1;
  font-family: ui-monospace, monospace;
  font-size: 12px;
  color: #fff;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.subfolders-list {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(140px, 1fr));
  gap: 6px;
  max-height: 140px;
  overflow-y: auto;
}
.folder-chip {
  padding: 4px 8px;
  background: #141c2b;
  border: 1px solid var(--line);
  border-radius: 4px;
  font-size: 11.5px;
  color: var(--fg);
  cursor: pointer;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  display: flex;
  align-items: center;
  gap: 4px;
}
.folder-chip:hover { border-color: var(--accent); background: #1c273c; color: #fff; }
.import-preview-card {
  background: #141c2b;
  border: 1px solid var(--line);
  border-radius: 6px;
  padding: 10px 12px;
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
    <button onclick="openStorageModal('sync')">📁 Storage & Folders</button>
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

<!-- Storage & Folders Modal -->
<div id="modal-storage" class="modal-overlay" style="display:none;" onclick="if(event.target===this)closeStorageModal()">
  <div class="modal-box">
    <div class="modal-header">
      <div style="display:flex; align-items:center; gap:8px;">
        <span style="font-size:16px;">📁</span>
        <h3 style="margin:0; font-size:15px; font-weight:600;">Storage & Folder Manager</h3>
      </div>
      <button class="btn-sm" onclick="closeStorageModal()">✕</button>
    </div>

    <!-- Modal Tabs -->
    <div class="modal-tabs">
      <button class="modal-tab-btn active" id="modal-tab-sync" onclick="switchStorageTab('sync')">Target Sync Folder</button>
      <button class="modal-tab-btn" id="modal-tab-library" onclick="switchStorageTab('library')">Library Storage</button>
      <button class="modal-tab-btn" id="modal-tab-import" onclick="switchStorageTab('import')">Import from Folder</button>
    </div>

    <div class="modal-body">
      <!-- Tab 1: Target Sync Folder -->
      <div id="storage-view-sync" class="storage-tab-view">
        <div class="folder-desc">Choose which SD card, USB drive, or folder to sync your music into for playback.</div>
        <div class="folder-current-box">
          <div class="folder-label">Current Sync Target:</div>
          <div class="folder-val" id="sync-current-val">Loading…</div>
        </div>

        <div class="folder-section-title">Detected Drives & Volumes</div>
        <div id="sync-detected-volumes" class="detected-volumes-list">
          <div style="color:var(--muted); font-size:12px;">Scanning detected volumes…</div>
        </div>

        <div class="folder-section-title" style="margin-top:14px;">Or Set Custom Sync Destination</div>
        <div class="folder-picker-bar">
          <input type="text" id="sync-custom-input" placeholder="/Volumes/MyCard or custom path" spellcheck="false">
          <button class="primary" onclick="applyCustomSyncFolder()">Set as Sync Folder</button>
        </div>
      </div>

      <!-- Tab 2: Library Storage Root -->
      <div id="storage-view-library" class="storage-tab-view" style="display:none;">
        <div class="folder-desc">Location where UnStream stores downloaded FLAC files, organized folders, and database.</div>
        <div class="folder-current-box">
          <div class="folder-label">Current Library Directory:</div>
          <div class="folder-val" id="lib-current-val">Loading…</div>
        </div>

        <div class="folder-section-title">Change Library Location</div>
        <div class="folder-picker-bar">
          <input type="text" id="lib-custom-input" placeholder="/Users/username/Music or custom path" spellcheck="false">
          <button class="primary" onclick="applyCustomLibraryFolder()">Set Library Folder</button>
        </div>
      </div>

      <!-- Tab 3: Import from Folder -->
      <div id="storage-view-import" class="storage-tab-view" style="display:none;">
        <div class="folder-desc">Scan any local folder with existing music (FLAC, MP3, etc.) to validate, tag, and import into your library.</div>
        
        <div class="folder-picker-bar">
          <input type="text" id="import-folder-input" placeholder="Select or type folder path (e.g. ~/Music)" spellcheck="false">
          <button class="btn-sm" onclick="scanSelectedImportFolder()">🔍 Preview Audio</button>
          <button class="primary" id="btn-run-import" onclick="executeFolderImport()">📥 Import into Library</button>
        </div>

        <!-- Scan Preview Card -->
        <div id="import-preview-box" style="display:none;" class="import-preview-card">
          <div id="import-preview-stats" style="font-weight:600; font-size:12.5px; margin-bottom:6px;"></div>
          <div id="import-preview-samples" style="font-size:11.5px; color:var(--muted); max-height:120px; overflow-y:auto;"></div>
        </div>
      </div>

      <!-- Interactive Folder Browser / Subfolder Explorer -->
      <div class="folder-browser-box">
        <div class="browser-header">
          <div style="font-size:11.5px; font-weight:600; color:var(--muted);">QUICK LOCATIONS:</div>
          <div id="quick-paths-list" class="quick-paths-row"></div>
        </div>
        <div class="browser-current-row">
          <button class="btn-sm" id="btn-folder-up" onclick="navigateFolderUp()" title="Go up to parent directory">⬆ Up</button>
          <span class="browser-path-display" id="browser-current-path">~</span>
          <button class="btn-sm" onclick="selectCurrentBrowserFolder()">Use This Folder</button>
        </div>
        <div class="subfolders-list" id="subfolders-list"></div>
      </div>
    </div>

    <div class="modal-footer">
      <span id="storage-modal-status" style="font-size:12px; color:var(--muted);"></span>
      <button onclick="closeStorageModal()">Close</button>
    </div>
  </div>
</div>

<main>
  <!-- Library Status -->
  <section>
    <div class="sec-head">
      <h2>Verified Library</h2>
      <div style="display:flex; gap:6px; align-items:center;">
        <span id="lib-status" class="badge ok">local</span>
        <button class="btn-sm" onclick="openStorageModal('library')" title="Change library folder">Folder…</button>
      </div>
    </div>
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
    <div class="sec-head">
      <h2>Target MicroSD Card</h2>
      <div style="display:flex; gap:6px; align-items:center;">
        <span id="card-badge" class="badge">detecting</span>
        <button class="btn-sm" onclick="openStorageModal('sync')" title="Select or change sync destination">Change…</button>
      </div>
    </div>
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
          <button class="tab-btn" id="tab-btn-discovery" onclick="switchBrowserTab('discovery')">✨ Discover New Music (<span id="discovery-tab-count">0</span>)</button>
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
        <div style="display:flex; align-items:center; gap:8px; flex-wrap:wrap;">
          <span id="songs-selected-counter" style="font-size: 11px; color: var(--accent); font-weight: 600;"></span>
          <button class="btn-sm primary" id="btn-dl-selected-songs" onclick="downloadSelectedSongs(this)" disabled style="display:none;">⬇ Download Selected</button>
          <button class="btn-sm primary" id="btn-dl-saved-all" onclick="downloadSavedAll(this)" style="display:none;">⬇ Download All Liked Songs</button>
          <button class="btn-sm" id="btn-song-prev" onclick="changeSongPage(-1)" disabled>◀ Previous</button>
          <span id="song-page-indicator" style="font-size:11px; color:var(--muted); font-variant-numeric:tabular-nums;">Page 1 of 1</span>
          <button class="btn-sm" id="btn-song-next" onclick="changeSongPage(1)" disabled>Next ▶</button>
        </div>
      </div>
      <div style="max-height: 480px; overflow-y: auto; border: 1px solid var(--line); border-radius: 6px;">
        <table id="songs-table">
          <thead>
            <tr>
              <th style="width: 32px;"><input type="checkbox" id="select-all-songs" onchange="toggleSelectAllSongs(this)" title="Select/Deselect All Songs on Page"></th>
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
            <tr><td colspan="8" class="empty-msg">Loading songs…</td></tr>
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

    <!-- Discovery View (Explore & Unowned Candidates) -->
    <div id="tab-view-discovery" style="display:none;">
      <!-- Discovery Action Toolbar & Status Filters -->
      <div style="margin-bottom: 12px; display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:10px;">
        <div style="display:flex; align-items:center; gap:8px; flex-wrap:wrap;">
          <span style="font-size:11px; color:var(--muted); font-weight:600; text-transform:uppercase; letter-spacing:0.5px;">Filter:</span>
          <button class="btn-sm active" id="btn-cand-filter-awaiting" onclick="setCandidateFilter('awaiting_approval')">Pending Review (<span id="count-cand-pending">0</span>)</button>
          <button class="btn-sm" id="btn-cand-filter-all" onclick="setCandidateFilter('all')">All Unowned (<span id="count-cand-all">0</span>)</button>
          <button class="btn-sm" id="btn-cand-filter-approved" onclick="setCandidateFilter('approved')">Approved (<span id="count-cand-approved">0</span>)</button>
          <button class="btn-sm" id="btn-cand-filter-rejected" onclick="setCandidateFilter('rejected')">Rejected (<span id="count-cand-rejected">0</span>)</button>
        </div>
        <div style="display:flex; align-items:center; gap:8px; flex-wrap:wrap;">
          <span id="selection-counter" style="font-size: 11px; color: var(--accent); font-weight: 600;"></span>
          <button class="btn-sm success" id="btn-approve-selected" onclick="approveSelected()" disabled>✓ Approve Selected</button>
          <button class="btn-sm danger" id="btn-reject-selected" onclick="rejectSelected()" disabled>✕ Reject</button>
          <button class="btn-sm" onclick="approveAllPending()">✓ Approve All Pending</button>
          <button class="btn-sm primary" id="btn-run-discovery" onclick="runDiscoveryScan(this)">⚡ Scan For New Music</button>
        </div>
      </div>

      <!-- Interactive Live Music Explorer Search Box -->
      <div style="background:#090e17; border:1px solid var(--line); border-radius:6px; padding:12px 14px; margin-bottom:14px;">
        <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:8px; flex-wrap:wrap; gap:8px;">
          <div style="display:flex; align-items:center; gap:8px;">
            <span style="font-size:12px; font-weight:600; color:var(--fg);">🔎 Live Artist & Genre Explorer</span>
            <span style="font-size:11px; color:var(--muted);">Search Spotify & public sources to preview & download unowned tracks in FLAC</span>
          </div>
          <span style="font-size:11px; color:var(--muted);">Press Enter or click Explore</span>
        </div>
        <div style="display:flex; gap:8px;">
          <input type="text" id="explore-input" placeholder="Search any artist, subgenre, or mood (e.g. Khruangbin, Krautrock, Bappi Lahiri, Synthwave, Post-Rock)…" onkeydown="if(event.key==='Enter') runExploreSearch()" style="background:#04070c; border:1px solid var(--line); border-radius:4px; padding:7px 12px; font-size:12px; color:#fff; flex:1; outline:none;">
          <button class="btn-sm primary" id="btn-explore-search" onclick="runExploreSearch()">Explore</button>
        </div>
        <div id="explore-results-box" style="display:none; margin-top:12px; max-height:280px; overflow-y:auto; border-top:1px solid var(--line); padding-top:10px;">
          <div id="explore-results-list" style="display:flex; flex-direction:column; gap:6px;"></div>
        </div>
      </div>

      <!-- Discovery Candidates Table -->
      <div style="max-height: 480px; overflow-y: auto; border: 1px solid var(--line); border-radius: 6px;">
        <table id="candidates-table">
          <thead>
            <tr>
              <th style="width: 32px;"><input type="checkbox" id="select-all-cands" onchange="toggleSelectAll(this)" title="Select/Deselect All Candidates"></th>
              <th style="width: 44px;">#</th>
              <th style="width: 44px;">Cover</th>
              <th>Release Title</th>
              <th>Artist</th>
              <th style="text-align:right;">Year</th>
              <th style="text-align:right;">Score</th>
              <th>Match Reason / Signals</th>
              <th>Status</th>
              <th style="width: 170px;">Action</th>
            </tr>
          </thead>
          <tbody id="candidates-body">
            <tr><td colspan="10" class="empty-msg">Loading discovery candidates…</td></tr>
          </tbody>
        </table>
      </div>
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
let selectedSongIds = new Set();
let currentSongsList = [];
let downloadedFilesData = [];
let currentCandFilter = 'awaiting_approval';
let candidateFilterQuery = '';

async function switchBrowserTab(tab) {
  currentBrowserTab = tab;
  document.querySelectorAll("#browser-tabs .tab-btn").forEach(b => b.classList.remove("active"));
  const activeBtn = document.getElementById(`tab-btn-${tab}`);
  if (activeBtn) activeBtn.classList.add("active");

  const plView = document.getElementById("tab-view-playlists");
  const songsView = document.getElementById("tab-view-songs");
  const dlView = document.getElementById("tab-view-downloaded");
  const discView = document.getElementById("tab-view-discovery");
  const filterInput = document.getElementById("browser-filter-input");
  const btnDlSavedAll = document.getElementById("btn-dl-saved-all");
  const btnDlSelected = document.getElementById("btn-dl-selected-songs");
  filterInput.value = "";
  songFilterQuery = "";
  candidateFilterQuery = "";
  selectedSongIds.clear();
  updateSongSelectionUI();

  if (tab === "playlists") {
    plView.style.display = "block";
    songsView.style.display = "none";
    if (dlView) dlView.style.display = "none";
    if (discView) discView.style.display = "none";
    if (btnDlSavedAll) btnDlSavedAll.style.display = "none";
    if (btnDlSelected) btnDlSelected.style.display = "none";
    filterInput.placeholder = "Filter playlists by name or curator…";
    loadPlaylists();
  } else if (tab === "saved") {
    plView.style.display = "none";
    songsView.style.display = "block";
    if (dlView) dlView.style.display = "none";
    if (discView) discView.style.display = "none";
    if (btnDlSavedAll) btnDlSavedAll.style.display = "inline-flex";
    filterInput.placeholder = "Filter liked songs…";
    document.getElementById("songs-count-label").textContent = "Showing Spotify Liked Songs";
    currentSongPage = 1;
    loadSongs();
  } else if (tab === "all_songs") {
    plView.style.display = "none";
    songsView.style.display = "block";
    if (dlView) dlView.style.display = "none";
    if (discView) discView.style.display = "none";
    if (btnDlSavedAll) btnDlSavedAll.style.display = "none";
    filterInput.placeholder = "Filter all songs by title, artist, or album…";
    document.getElementById("songs-count-label").textContent = "Showing All Library Songs";
    currentSongPage = 1;
    loadSongs();
  } else if (tab === "downloaded") {
    plView.style.display = "none";
    songsView.style.display = "none";
    if (dlView) dlView.style.display = "block";
    if (discView) discView.style.display = "none";
    if (btnDlSavedAll) btnDlSavedAll.style.display = "none";
    if (btnDlSelected) btnDlSelected.style.display = "none";
    filterInput.placeholder = "Filter downloaded songs by title, artist, or album…";
    loadDownloadedFiles();
  } else if (tab === "discovery") {
    plView.style.display = "none";
    songsView.style.display = "none";
    if (dlView) dlView.style.display = "none";
    if (discView) discView.style.display = "block";
    if (btnDlSavedAll) btnDlSavedAll.style.display = "none";
    if (btnDlSelected) btnDlSelected.style.display = "none";
    filterInput.placeholder = "Filter candidates by release, artist, or reason…";
    loadDiscoveryCandidates();
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
    tbody.innerHTML = `<tr><td colspan="8" class="empty-msg">Error loading songs: ${data ? data.error : "network"}</td></tr>`;
    return;
  }
  currentSongsList = data.songs || [];
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

  if (!currentSongsList.length) {
    tbody.innerHTML = `<tr><td colspan="8" class="empty-msg">No songs found matching query</td></tr>`;
    updateSongSelectionUI();
    return;
  }

  const offset = (data.page - 1) * 50;
  tbody.innerHTML = currentSongsList.map((s, idx) => {
    const isChecked = selectedSongIds.has(s.id);
    const isReady = !!s.validated;
    return `
      <tr>
        <td onclick="event.stopPropagation()">
          <input type="checkbox" data-song-id="${s.id}" ${isChecked ? "checked" : ""} onchange="onSongSelectChange(${s.id}, this.checked)">
        </td>
        <td style="color:var(--muted);">${offset + idx + 1}</td>
        <td><b>${escapeHtml(s.title)}</b></td>
        <td>${escapeHtml(s.artist_name)}</td>
        <td>${escapeHtml(s.album_title)}</td>
        <td style="text-align:right;">${msToMin(s.duration_ms)}</td>
        <td>${isReady ? '<span class="badge ok">✓ On Disk / Ready</span>' : (s.path ? '<span class="badge sync">Local Matched</span>' : '<span class="badge">Spotify Cloud</span>')}</td>
        <td>
          ${isReady ? '<span class="badge ok">✓ Ready</span>' : `<button class="btn-sm" onclick="downloadSongRow(${s.id}, this)">⬇ Download</button>`}
        </td>
      </tr>
    `;
  }).join("");
  updateSongSelectionUI();
}

function onSongSelectChange(id, checked) {
  if (checked) {
    selectedSongIds.add(id);
  } else {
    selectedSongIds.delete(id);
  }
  updateSongSelectionUI();
}

function toggleSelectAllSongs(master) {
  if (master.checked) {
    currentSongsList.forEach(s => selectedSongIds.add(s.id));
  } else {
    selectedSongIds.clear();
  }
  loadSongs();
}

function updateSongSelectionUI() {
  const count = selectedSongIds.size;
  const counter = document.getElementById("songs-selected-counter");
  const btnSelected = document.getElementById("btn-dl-selected-songs");
  const master = document.getElementById("select-all-songs");
  if (counter) counter.textContent = count ? `(${count} songs selected)` : "";
  if (btnSelected) {
    btnSelected.disabled = count === 0;
    btnSelected.style.display = count > 0 ? "inline-flex" : "none";
  }
  if (master && currentSongsList.length > 0) {
    master.checked = count === currentSongsList.length && count > 0;
    master.indeterminate = count > 0 && count < currentSongsList.length;
  }
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
    } else if (currentBrowserTab === "discovery") {
      candidateFilterQuery = val.trim();
      loadDiscoveryCandidates();
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

async function downloadSavedAll(btn) {
  if (!confirm("Download ALL Spotify Liked Songs into your local FLAC music library?")) return;
  if (btn) {
    btn.disabled = true;
    btn.textContent = "⏳ Downloading All Liked…";
  }
  showToast("Downloading all Spotify Liked Songs…");
  pollActivity();
  try {
    const res = await fetch("/api/actions/download-saved?limit=0", { method: "POST" });
    const data = await res.json();
    if (data.ok) {
      showToast(`All Liked Songs completed: ${data.downloaded} new tracks downloaded, ${data.skipped} already present!`);
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
      btn.textContent = "⬇ Download All Liked Songs";
    }
    pollActivity();
  }
}

async function downloadSelectedSongs(btn) {
  const ids = Array.from(selectedSongIds);
  if (!ids.length) return;
  if (btn) {
    btn.disabled = true;
    btn.textContent = "⏳ Downloading Selected…";
  }
  showToast(`Downloading ${ids.length} selected tracks in FLAC…`);
  pollActivity();
  try {
    const res = await fetch(`/api/actions/download-tracks-batch?track_ids=${ids.join(",")}`, { method: "POST" });
    const data = await res.json();
    if (data.ok) {
      showToast(`Batch completed: ${data.downloaded} new tracks downloaded, ${data.skipped} already present!`);
      selectedSongIds.clear();
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
      btn.textContent = "⬇ Download Selected";
    }
    pollActivity();
  }
}

const downloadSavedBatch = downloadSavedAll;

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

function setCandidateFilter(status) {
  currentCandFilter = status;
  ["awaiting", "all", "approved", "rejected"].forEach(k => {
    const el = document.getElementById(`btn-cand-filter-${k}`);
    if (el) el.classList.remove("active");
  });
  const activeBtn = document.getElementById(
    status === "awaiting_approval" ? "btn-cand-filter-awaiting" :
    status === "approved" ? "btn-cand-filter-approved" :
    status === "rejected" ? "btn-cand-filter-rejected" : "btn-cand-filter-all"
  );
  if (activeBtn) activeBtn.classList.add("active");
  loadDiscoveryCandidates();
}

async function loadDiscoveryCandidates() {
  const tbody = document.getElementById("candidates-body");
  const data = await safeFetch(`/api/candidates?status=${currentCandFilter}&q=${encodeURIComponent(candidateFilterQuery)}`);
  if (!data || data.error) {
    tbody.innerHTML = `<tr><td colspan="10" class="empty-msg">Error loading candidates: ${data ? data.error : "network"}</td></tr>`;
    return;
  }
  allCurrentCandidates = data.candidates || [];
  const counts = data.counts || {};

  const elPending = document.getElementById("count-cand-pending");
  const elAll = document.getElementById("count-cand-all");
  const elApp = document.getElementById("count-cand-approved");
  const elRej = document.getElementById("count-cand-rejected");
  const tabBadge = document.getElementById("discovery-tab-count");
  if (elPending) elPending.textContent = fmt(counts.awaiting_approval || 0);
  if (elAll) elAll.textContent = fmt(counts.all || 0);
  if (elApp) elApp.textContent = fmt(counts.approved || 0);
  if (elRej) elRej.textContent = fmt(counts.rejected || 0);
  if (tabBadge) tabBadge.textContent = fmt(counts.awaiting_approval || 0);

  renderCandidatesTable();
}

function renderCandidatesTable() {
  const candBody = document.getElementById("candidates-body");
  if (!allCurrentCandidates.length) {
    candBody.innerHTML = `<tr><td colspan="10" class="empty-msg">No discovery candidates found matching filter</td></tr>`;
    updateSelectionUI();
    return;
  }
  candBody.innerHTML = allCurrentCandidates.map((c, idx) => {
    const isApproved = c.status === "approved";
    const isChecked = selectedCandidateIds.has(c.id);
    const reasonsStr = (c.reasons || []).map(r => r.text || r.reason || r.code).join("; ");
    const coverImg = c.cover_url
      ? `<img src="${escapeHtml(c.cover_url)}" class="cand-cover" alt="cover">`
      : `<div class="cand-cover" style="display:flex;align-items:center;justify-content:center;color:var(--muted);font-size:9px;font-weight:600;">FLAC</div>`;
    const scoreVal = c.score ? `<span class="badge" style="color:var(--accent); font-weight:600;">${c.score.toFixed(1)}</span>` : "-";
    const statusBadge = isApproved
      ? `<span class="badge ok">Approved</span>`
      : (c.status === "rejected" ? `<span class="badge danger">Rejected</span>` : `<span class="badge warn">Pending Review</span>`);

    return `
      <tr>
        <td><input type="checkbox" data-id="${c.id}" ${isChecked ? "checked" : ""} onchange="onRowSelectChange(${c.id}, this.checked)"></td>
        <td style="color:var(--muted); font-size:11px;">#${c.id}</td>
        <td>${coverImg}</td>
        <td>
          <b>${escapeHtml(c.release_title || c.entity_name || "Untitled")}</b>
          <span class="badge" style="font-size:9px; text-transform:uppercase; margin-left:4px;">${c.entity_type || "ALBUM"}</span>
        </td>
        <td>${escapeHtml(c.artist_name || "Unknown")}</td>
        <td style="text-align:right; font-variant-numeric:tabular-nums;">${c.release_year || "-"}</td>
        <td style="text-align:right; font-variant-numeric:tabular-nums;">${scoreVal}</td>
        <td style="color:var(--muted); font-size:11px; max-width:240px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis;" title="${escapeHtml(reasonsStr)}">${escapeHtml(reasonsStr || "Taste profile match")}</td>
        <td>${statusBadge}</td>
        <td>
          <div style="display:flex; gap:5px; align-items:center;">
            <button class="btn-sm primary" onclick="downloadCandidateRow(${c.id}, this)">⬇ Download</button>
            ${!isApproved ? `<button class="btn-sm success" onclick="approveCandidate(${c.id})" title="Approve candidate">✓</button>` : ""}
            ${c.status !== "rejected" ? `<button class="btn-sm danger" onclick="rejectCandidate(${c.id})" title="Reject candidate">✕</button>` : ""}
          </div>
        </td>
      </tr>
    `;
  }).join("");
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

  if (countEl) countEl.textContent = count ? `(${count} selected)` : "";
  if (btnApprove) btnApprove.disabled = count === 0;
  if (btnReject) btnReject.disabled = count === 0;
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
    loadDiscoveryCandidates();
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
    loadDiscoveryCandidates();
    refreshAll();
  }
}

async function approveAllPending() {
  if (!confirm(`Are you sure you want to approve all pending candidates?`)) return;
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
    loadDiscoveryCandidates();
    refreshAll();
  }
}

async function downloadCandidateRow(candidateId, btn) {
  if (btn) {
    btn.disabled = true;
    btn.textContent = "⏳ Downloading…";
  }
  showToast("Downloading discovery candidate in FLAC…");
  pollActivity();
  try {
    const res = await fetch(`/api/actions/download-candidate?candidate_id=${candidateId}`, { method: "POST" });
    const data = await res.json();
    if (data.ok) {
      showToast(`✓ Download complete: '${data.title || "Track"}' is ready on disk & added to FiiO card!`);
      loadDiscoveryCandidates();
      loadSongs();
      refreshAll();
    } else {
      showToast("Download failed: " + (data.error || "failed"), true);
    }
  } catch (err) {
    showToast("Error: " + err.message, true);
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.textContent = "⬇ Download";
    }
    pollActivity();
  }
}

async function runDiscoveryScan(btn) {
  if (btn) {
    btn.disabled = true;
    btn.textContent = "⏳ Scanning…";
  }
  showToast("Scanning Spotify, MusicBrainz, and Internet Archive for unowned music…");
  pollActivity();
  try {
    const res = await fetch("/api/actions/discover?limit_artists=10", { method: "POST" });
    const data = await res.json();
    if (data.ok) {
      const summary = data.summary || {};
      showToast(`✓ Discovery scan complete: found ${summary.albums || 0} unowned releases! (${data.pending} pending review)`);
      loadDiscoveryCandidates();
      refreshAll();
    } else {
      showToast("Discovery scan error: " + (data.error || "failed"), true);
    }
  } catch (err) {
    showToast("Error: " + err.message, true);
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.textContent = "⚡ Scan For New Music";
    }
    pollActivity();
  }
}

async function runExploreSearch() {
  const input = document.getElementById("explore-input");
  const query = input.value.trim();
  if (!query) return;
  const box = document.getElementById("explore-results-box");
  const list = document.getElementById("explore-results-list");
  const btn = document.getElementById("btn-explore-search");
  if (btn) {
    btn.disabled = true;
    btn.textContent = "Exploring…";
  }
  box.style.display = "block";
  list.innerHTML = `<span style="color:var(--muted); font-size:12px; padding:6px 0;">Searching public music sources for "${escapeHtml(query)}"…</span>`;
  try {
    const res = await fetch(`/api/discovery/explore?q=${encodeURIComponent(query)}`);
    const data = await res.json();
    const results = data.results || [];
    if (!results.length) {
      list.innerHTML = `<span style="color:var(--muted); font-size:12px; padding:6px 0;">No discoverable tracks found for "${escapeHtml(query)}"</span>`;
      return;
    }
    list.innerHTML = results.map(t => {
      const coverHtml = t.cover_url
        ? `<img src="${escapeHtml(t.cover_url)}" style="width:36px; height:36px; border-radius:3px; object-fit:cover;" alt="cover">`
        : `<div style="width:36px; height:36px; border-radius:3px; background:#141c2b; display:flex; align-items:center; justify-content:center; color:var(--muted); font-size:9px;">FLAC</div>`;
      const isReady = t.is_ready;
      return `
        <div class="explore-item">
          <div style="display:flex; align-items:center; gap:10px;">
            ${coverHtml}
            <div>
              <div style="font-weight:600; color:#fff;">${escapeHtml(t.title)}</div>
              <div style="color:var(--muted); font-size:11px;">${escapeHtml(t.artist_name)} • ${escapeHtml(t.album_title || "Single")} • ${t.release_date ? t.release_date.slice(0, 4) : ""}</div>
            </div>
          </div>
          <div>
            ${isReady
              ? '<span class="badge ok">✓ In Library</span>'
              : `<button class="btn-sm primary" onclick="downloadExploreTrackRow(this, '${escapeHtml(t.title).replace(/'/g, "\\'")}', '${escapeHtml(t.artist_name).replace(/'/g, "\\'")}', '${escapeHtml(t.album_title || "Single").replace(/'/g, "\\'")}', '${t.spotify_id || ""}', '${t.cover_url || ""}')">⬇ Download (FLAC)</button>`
            }
          </div>
        </div>
      `;
    }).join("");
  } catch (err) {
    list.innerHTML = `<span style="color:var(--bad); font-size:12px;">Exploration error: ${err.message}</span>`;
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.textContent = "Explore";
    }
  }
}

async function downloadExploreTrackRow(btn, title, artist, album, spotifyId, coverUrl) {
  if (btn) {
    btn.disabled = true;
    btn.textContent = "⏳ Downloading…";
  }
  showToast(`Downloading newly discovered track: '${title}'…`);
  pollActivity();
  try {
    const params = new URLSearchParams({ title, artist, album });
    if (spotifyId) params.append("spotify_id", spotifyId);
    if (coverUrl) params.append("cover_url", coverUrl);
    const res = await fetch(`/api/actions/download-explore-track?${params.toString()}`, { method: "POST" });
    const data = await res.json();
    if (data.ok) {
      showToast(`✓ Downloaded and verified: '${title}' by ${artist} is ready on disk!`);
      if (btn) {
        btn.outerHTML = '<span class="badge ok">✓ In Library</span>';
      }
      loadSongs();
      refreshAll();
    } else {
      showToast("Download failed: " + (data.error || "failed"), true);
      if (btn) {
        btn.disabled = false;
        btn.textContent = "⬇ Download (FLAC)";
      }
    }
  } catch (err) {
    showToast("Error: " + err.message, true);
    if (btn) {
      btn.disabled = false;
      btn.textContent = "⬇ Download (FLAC)";
    }
  } finally {
    pollActivity();
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

// ------------------------------------------------------------- Storage & Folders
let currentStorageTab = 'sync';
let currentBrowserPath = '';
let currentBrowserParent = '';

function openStorageModal(tab = 'sync') {
  const modal = document.getElementById('modal-storage');
  if (!modal) return;
  modal.style.display = 'flex';
  switchStorageTab(tab);
  loadStorageOverview();
  browseFolder(currentBrowserPath || '');
}

function closeStorageModal() {
  const modal = document.getElementById('modal-storage');
  if (modal) modal.style.display = 'none';
}

function switchStorageTab(tab) {
  currentStorageTab = tab;
  ['sync', 'library', 'import'].forEach(t => {
    const btn = document.getElementById('modal-tab-' + t);
    const view = document.getElementById('storage-view-' + t);
    if (btn) btn.classList.toggle('active', t === tab);
    if (view) view.style.display = (t === tab) ? 'block' : 'none';
  });
}

async function loadStorageOverview() {
  try {
    const statusData = await safeFetch('/api/status');
    const devData = await safeFetch('/api/devices');

    if (statusData && statusData.storage) {
      document.getElementById('lib-current-val').textContent = statusData.storage.library_root || 'Not configured';
      document.getElementById('lib-custom-input').value = statusData.storage.library_root || '';
      const syncTarget = statusData.storage.target_mount_point || (devData && devData.volumes && devData.volumes[0] ? devData.volumes[0].mount_point : 'Auto-detected / Not set');
      document.getElementById('sync-current-val').textContent = syncTarget;
      document.getElementById('sync-custom-input').value = statusData.storage.target_mount_point || (devData && devData.volumes && devData.volumes[0] ? devData.volumes[0].mount_point : '');
    }

    // Render detected volumes
    const volBox = document.getElementById('sync-detected-volumes');
    if (volBox && devData) {
      const vols = devData.volumes || [];
      if (!vols.length) {
        volBox.innerHTML = '<div style="color:var(--muted); font-size:12px; padding:4px 0;">No external cards or USB drives detected. You can set any custom path below.</div>';
      } else {
        volBox.innerHTML = vols.map(v => `
          <div class="volume-item ${v.confirmed ? 'active' : ''}" onclick="applyDetectedVolume('${escapeHtml(v.mount_point)}')">
            <div>
              <div style="font-weight:600; color:#fff;">${escapeHtml(v.volume_name || 'Volume')} <span style="font-size:11px; color:var(--muted);">(${v.capacity_gb} GB • ${v.filesystem})</span></div>
              <div style="font-family:ui-monospace,monospace; font-size:11px; color:var(--accent);">${escapeHtml(v.mount_point)}</div>
            </div>
            <button class="btn-sm ${v.confirmed ? 'primary' : ''}">Select</button>
          </div>
        `).join('');
      }
    }
  } catch (err) {
    console.error('Failed to load storage overview', err);
  }
}

async function browseFolder(path) {
  try {
    const res = await fetch(`/api/system/folders?path=${encodeURIComponent(path || '')}`);
    const data = await res.json();
    currentBrowserPath = data.current_path;
    currentBrowserParent = data.parent_path;

    const pathDisp = document.getElementById('browser-current-path');
    if (pathDisp) pathDisp.textContent = data.current_path;

    const btnUp = document.getElementById('btn-folder-up');
    if (btnUp) btnUp.disabled = !data.parent_path;

    // Quick paths
    const quickBox = document.getElementById('quick-paths-list');
    if (quickBox && data.quick_paths) {
      quickBox.innerHTML = data.quick_paths.map(q => `
        <span class="quick-chip" onclick="browseFolder('${escapeHtml(q.path)}')">${escapeHtml(q.label)}</span>
      `).join('');
    }

    // Subdirectories
    const subBox = document.getElementById('subfolders-list');
    if (subBox) {
      const subs = data.subdirectories || [];
      if (!subs.length) {
        subBox.innerHTML = '<span style="color:var(--muted); font-size:11.5px; padding:4px;">No visible subfolders</span>';
      } else {
        subBox.innerHTML = subs.map(s => `
          <div class="folder-chip" onclick="browseFolder('${escapeHtml(s.path)}')" title="${escapeHtml(s.path)}">
            📁 ${escapeHtml(s.name)}
          </div>
        `).join('');
      }
    }

    // If import tab is active, update input automatically
    if (currentStorageTab === 'import') {
      const impInp = document.getElementById('import-folder-input');
      if (impInp && !impInp.value) impInp.value = data.current_path;
    }
  } catch (err) {
    console.error('Browse folder error:', err);
  }
}

function navigateFolderUp() {
  if (currentBrowserParent) {
    browseFolder(currentBrowserParent);
  }
}

function selectCurrentBrowserFolder() {
  if (currentStorageTab === 'sync') {
    document.getElementById('sync-custom-input').value = currentBrowserPath;
    applyCustomSyncFolder();
  } else if (currentStorageTab === 'library') {
    document.getElementById('lib-custom-input').value = currentBrowserPath;
    applyCustomLibraryFolder();
  } else if (currentStorageTab === 'import') {
    document.getElementById('import-folder-input').value = currentBrowserPath;
    scanSelectedImportFolder();
  }
}

async function applyCustomSyncFolder() {
  const path = document.getElementById('sync-custom-input').value.trim();
  if (!path) return;
  showToast(`Setting target sync folder to ${path}…`);
  try {
    const res = await fetch(`/api/settings/target-device?mount_point=${encodeURIComponent(path)}&confirm=true`, { method: 'POST' });
    const data = await res.json();
    if (data.ok) {
      showToast(`✓ Sync target set to: ${data.mount_point}`);
      loadStorageOverview();
      refreshAll();
    } else {
      showToast('Error: ' + data.error, true);
    }
  } catch (err) {
    showToast('Failed to set target: ' + err.message, true);
  }
}

async function applyDetectedVolume(mountPoint) {
  document.getElementById('sync-custom-input').value = mountPoint;
  applyCustomSyncFolder();
}

async function applyCustomLibraryFolder() {
  const path = document.getElementById('lib-custom-input').value.trim();
  if (!path) return;
  if (!confirm(`Change music library directory to:\n${path}\n\nExisting files in the current library won't be deleted.`)) return;
  showToast(`Updating library storage location…`);
  try {
    const res = await fetch(`/api/settings/library-root?path=${encodeURIComponent(path)}`, { method: 'POST' });
    const data = await res.json();
    if (data.ok) {
      showToast(`✓ Library directory updated to: ${data.library_root}`);
      loadStorageOverview();
      refreshAll();
    } else {
      showToast('Error: ' + data.error, true);
    }
  } catch (err) {
    showToast('Failed to update library root: ' + err.message, true);
  }
}

async function scanSelectedImportFolder() {
  const input = document.getElementById('import-folder-input');
  const path = input.value.trim() || currentBrowserPath;
  if (!path) return;
  input.value = path;
  const box = document.getElementById('import-preview-box');
  const stats = document.getElementById('import-preview-stats');
  const samples = document.getElementById('import-preview-samples');

  box.style.display = 'block';
  stats.innerHTML = `<span style="color:var(--muted);">Scanning ${escapeHtml(path)} for audio files…</span>`;
  samples.innerHTML = '';

  try {
    const res = await fetch(`/api/folder/scan?path=${encodeURIComponent(path)}`);
    const data = await res.json();
    if (data.ok && data.summary) {
      const s = data.summary;
      const exts = Object.entries(s.extensions || {}).map(([k, v]) => `${k.toUpperCase()}: ${v}`).join(', ');
      stats.innerHTML = `Found <span style="color:var(--good);">${s.total_files} audio files</span> (${s.total_mb} MB) ${exts ? `• [${exts}]` : ''}`;
      if (s.sample_tracks && s.sample_tracks.length) {
        samples.innerHTML = s.sample_tracks.map(t => `
          <div style="padding:2px 0;">• <span style="color:#fff;">${escapeHtml(t.title)}</span> <span style="color:var(--muted);">by ${escapeHtml(t.artist)} (${escapeHtml(t.album)})</span></div>
        `).join('');
      } else {
        samples.innerHTML = '<span style="color:var(--muted);">No audio files found in this folder.</span>';
      }
    } else {
      stats.innerHTML = `<span style="color:var(--bad);">Scan failed: ${escapeHtml(data.error || 'Unknown error')}</span>`;
    }
  } catch (err) {
    stats.innerHTML = `<span style="color:var(--bad);">Error scanning folder: ${escapeHtml(err.message)}</span>`;
  }
}

async function executeFolderImport() {
  const input = document.getElementById('import-folder-input');
  const path = input.value.trim() || currentBrowserPath;
  if (!path) {
    showToast('Please select a folder to import', true);
    return;
  }
  const btn = document.getElementById('btn-run-import');
  if (btn) {
    btn.disabled = true;
    btn.textContent = 'Importing…';
  }
  showToast(`Starting folder import from ${path}… (check Live Activity monitor)`);
  pollActivity();

  try {
    const res = await fetch(`/api/actions/import-folder?folder_path=${encodeURIComponent(path)}&copy_files=true`, { method: 'POST' });
    const data = await res.json();
    if (data.ok) {
      showToast(`✓ Import complete: ${data.imported} tracks imported into library (${data.skipped} skipped)`);
      refreshAll();
    } else {
      showToast('Import error: ' + (data.error || 'failed'), true);
    }
  } catch (err) {
    showToast('Import request error: ' + err.message, true);
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.textContent = '📥 Import into Library';
    }
    pollActivity();
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
