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

    @app.get("/api/search")
    def search(q: str = Query(default="")) -> dict[str, Any]:
        query = q.strip()
        if not query:
            return {"query": "", "tracks": [], "artists": [], "candidates": []}
        pattern = f"%{query}%"
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
        return {"query": query, "tracks": tracks, "artists": artists, "candidates": candidates}

    @app.post("/api/actions/sync")
    def action_sync(dry_run: bool = Query(default=True)) -> dict[str, Any]:
        try:
            planner = SyncPlanner(config, db)
            plan = planner.plan()
            if not dry_run and plan.fits:
                result = planner.execute(plan, dry_run=False)
                return {"ok": True, "action": "sync", "result": result}
            return {"ok": True, "action": "plan", "plan": plan.as_dict()}
        except Exception as exc:
            logger.exception("Dashboard sync action failed")
            return {"ok": False, "error": str(exc)}

    @app.post("/api/actions/scan")
    def action_scan() -> dict[str, Any]:
        try:
            scanner = LibraryScanner(config, db)
            summary = scanner.scan()
            return {"ok": True, "summary": summary.as_dict()}
        except Exception as exc:
            logger.exception("Dashboard scan action failed")
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

<footer>
  <span>Local loopback only (127.0.0.1). Hardware limits strictly checked against Echo Mini.</span>
  <span>Auto-refreshes every 10 seconds</span>
</footer>

<script>
const fmt = n => n == null ? "-" : new Intl.NumberFormat().format(n);
const gb = n => (n == null || isNaN(n)) ? "0.00 GB" : (n/1024/1024/1024).toFixed(2) + " GB";
const msToMin = ms => ms ? (Math.floor(ms/60000) + ":" + String(Math.floor((ms%60000)/1000)).padStart(2,"0")) : "-";
const kv = (label, value, cls="") => `<div class="kv"><span>${label}</span><b class="${cls}">${value}</b></div>`;

let allCurrentCandidates = [];
let selectedCandidateIds = new Set();
let searchDebounceTimer = null;

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

  // Pipeline
  if (s && !s.error && s.counts) {
    const c = s.counts;
    document.getElementById("pipeline").innerHTML =
      kv("Total Candidates", fmt(c.candidates)) +
      kv("Awaiting Review", fmt(c.awaiting_approval), c.awaiting_approval ? "warn" : "") +
      kv("Approved Acquisitions", fmt(c.acquisitions)) +
      kv("Total Sync Runs", fmt(c.sync_runs)) +
      kv("Reserved Capacity", "1.50 GB (Protected)");
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

  // Matching Library Tracks
  if (data.tracks && data.tracks.length) {
    html += `<h3 style="font-size:12px; text-transform:uppercase; color:var(--accent); margin:10px 0 6px;">Songs & Library Tracks (${data.tracks.length})</h3>`;
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

  if (!data.tracks.length && !data.artists.length && !data.candidates.length) {
    html = `<div class="empty-msg">No artists, songs, or candidates matched "${q}"</div>`;
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
</script>
</body>
</html>
"""
