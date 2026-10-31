"""Local-only FastAPI dashboard (127.0.0.1, read-only JSON + static page)."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse

from fiiolib.automation.scheduler import JOB_NAMES, Scheduler
from fiiolib.config import Config
from fiiolib.db import Database
from fiiolib.device.detect import DeviceError, DeviceLocator
from fiiolib.discovery.engine import candidate_rows
from fiiolib.library.queries import library_stats
from fiiolib.library.reports import report_payload
from fiiolib.spotify.auth import SpotifyAuth
from fiiolib.spotify.sync import status_summary
from fiiolib.spotify.taste import PROFILE_KEY

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
                "SELECT a.id, a.status, a.updated_at, r.title, r.artist_credit, r.licence "
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
        capabilities["devices"] = [volume.as_dict() for volume in volumes]
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
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>FiiO Library Manager</title>
<style>
:root { color-scheme: dark; --bg:#0d1117; --panel:#161b22; --line:#30363d; --fg:#e6edf3; --muted:#8b949e; --accent:#2f81f7; --good:#3fb950; --warn:#d29922; --bad:#f85149; }
* { box-sizing: border-box; }
body { margin:0; font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; background:var(--bg); color:var(--fg); }
header { padding:20px 28px; border-bottom:1px solid var(--line); display:flex; gap:16px; align-items:baseline; }
header h1 { margin:0; font-size:18px; letter-spacing:.02em; }
header span { color:var(--muted); font-size:12px; }
main { padding:24px 28px; display:grid; gap:20px; grid-template-columns:repeat(auto-fit,minmax(320px,1fr)); }
section { background:var(--panel); border:1px solid var(--line); border-radius:10px; padding:16px 18px; }
h2 { margin:0 0 12px; font-size:13px; text-transform:uppercase; letter-spacing:.08em; color:var(--muted); }
.kv { display:flex; justify-content:space-between; gap:12px; padding:4px 0; border-bottom:1px dashed #21262d; }
.kv:last-child { border:0; }
.kv b { font-weight:600; font-variant-numeric:tabular-nums; }
table { width:100%; border-collapse:collapse; font-size:12px; }
td,th { text-align:left; padding:5px 6px; border-bottom:1px solid #21262d; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; max-width:260px; }
th { color:var(--muted); font-weight:500; }
.badge { display:inline-block; padding:1px 7px; border-radius:999px; font-size:11px; border:1px solid var(--line); }
.ok { color:var(--good); } .warn { color:var(--warn); } .bad { color:var(--bad); }
footer { padding:8px 28px 24px; color:var(--muted); font-size:11px; }
</style></head>
<body>
<header><h1>FiiO Library Manager</h1><span id="stamp">loading…</span></header>
<main>
  <section><h2>Library</h2><div id="library"></div></section>
  <section><h2>Spotify</h2><div id="spotify"></div></section>
  <section><h2>Devices</h2><div id="devices"></div></section>
  <section><h2>Pipeline</h2><div id="pipeline"></div></section>
  <section><h2>Recent sync</h2><table id="sync"></table></section>
  <section><h2>Acquisitions</h2><table id="downloads"></table></section>
  <section><h2>Automation</h2><table id="automation"></table></section>
</main>
<footer>Local-only dashboard. Read-only. Refreshed automatically every 15 s.</footer>
<script>
const fmt = n => n == null ? "-" : new Intl.NumberFormat().format(n);
const gb = n => (n/1024/1024/1024).toFixed(2) + " GB";
const kv = (label, value, cls="") => `<div class="kv"><span>${label}</span><b class="${cls}">${value}</b></div>`;
async function json(url){ const r = await fetch(url); if(!r.ok) throw new Error(url+" "+r.status); return r.json(); }
async function refresh(){
  try {
    const [s, lib, dev, syn, dl, auto] = await Promise.all([
      json("/api/status"), json("/api/library"), json("/api/devices"), json("/api/sync"),
      json("/api/downloads"), json("/api/automation")
    ]);
    document.getElementById("library").innerHTML =
      kv("Files", fmt(lib.files)) + kv("Verified FLAC", fmt(lib.verified_flac)) +
      kv("Albums / artists", fmt(lib.albums)+" / "+fmt(lib.artists)) +
      kv("Size", gb(lib.size_bytes)) + kv("Transcode suspects", fmt(lib.transcode_suspect), lib.transcode_suspect?"warn":"");
    const sp = s.spotify || {};
    const sc = sp.counts || {};
    document.getElementById("spotify").innerHTML =
      kv("Connected", sp.logged_in ? "yes" : "no", sp.logged_in?"ok":"bad") +
      kv("Saved tracks", fmt(sc.saved_tracks)) + kv("Playlists", fmt(sc.playlists)) +
      kv("Taste artists", fmt(sp.profile_artists)) + kv("Last sync", sp.last_sync || "never");
    const dv = dev.volumes || [];
    document.getElementById("devices").innerHTML = dv.length ? dv.map(v =>
      kv(v.volume_name+" · "+v.filesystem, v.capacity_gb+" GB" + (v.confirmed?" · confirmed":" · unconfirmed"), v.confirmed?"ok":"warn")
    ).join("") + (dev.error?kv("Note", dev.error, "warn"):"") : kv("Card", "not mounted", "warn");
    const c = s.counts || {};
    document.getElementById("pipeline").innerHTML =
      kv("Candidates", fmt(c.candidates)) + kv("Awaiting approval", fmt(c.awaiting_approval), c.awaiting_approval?"warn":"") +
      kv("Acquisitions", fmt(c.acquisitions)) + kv("Sync runs", fmt(c.sync_runs));
    document.getElementById("sync").innerHTML = "<tr><th>#</th><th>When</th><th>Status</th><th>Added</th><th>Skipped</th></tr>" +
      (syn.runs||[]).slice(0,8).map(r => `<tr><td>${r.id}</td><td>${(r.started_at||"").slice(0,16)}</td><td>${r.status}</td><td>${fmt(r.added)}</td><td>${fmt(r.skipped)}</td></tr>`).join("");
    document.getElementById("downloads").innerHTML = "<tr><th>#</th><th>Release</th><th>Status</th></tr>" +
      (dl.acquisitions||[]).slice(0,8).map(a => `<tr><td>${a.id}</td><td>${(a.title||"").slice(0,40)}</td><td>${a.status}</td></tr>`).join("");
    document.getElementById("automation").innerHTML = "<tr><th>Job</th><th>Schedule</th><th>Last</th></tr>" +
      (auto.jobs||[]).map(j => `<tr><td>${j.name}</td><td>${j.schedule}</td><td>${(j.last_run||"never").slice(0,16)}</td></tr>`).join("");
    document.getElementById("stamp").textContent = "updated " + new Date().toLocaleTimeString();
  } catch (err) {
    document.getElementById("stamp").textContent = "error: " + err.message;
  }
}
refresh(); setInterval(refresh, 15000);
</script>
</body></html>
"""
