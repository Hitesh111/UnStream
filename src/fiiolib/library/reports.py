"""Human-readable and machine-readable library reports."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fiiolib.config import Config
from fiiolib.db import Database
from fiiolib.discovery.engine import candidate_rows
from fiiolib.library.dedupe import Deduplicator
from fiiolib.library.queries import library_stats


def report_payload(config: Config, db: Database) -> dict[str, Any]:
    stats = library_stats(db)
    duplicates = Deduplicator(config, db).find()
    sync_runs = [
        dict(row)
        for row in db.query(
            "SELECT id, started_at, finished_at, dry_run, status, added, updated, removed, "
            "skipped, planned_bytes, copied_bytes FROM sync_runs ORDER BY id DESC LIMIT 10"
        )
    ]
    downloads = {
        "awaiting_approval": int(db.scalar("SELECT COUNT(*) FROM acquisitions WHERE status = 'awaiting_approval'") or 0),
        "queued": int(db.scalar("SELECT COUNT(*) FROM acquisitions WHERE status = 'queued'") or 0),
        "active_torrents": int(db.scalar("SELECT COUNT(*) FROM torrent_jobs WHERE status IN ('queued','downloading','paused')") or 0),
        "completed": int(db.scalar("SELECT COUNT(*) FROM acquisitions WHERE status IN ('downloaded','verified','organised')") or 0),
    }
    candidates = candidate_rows(db, limit=15)
    return {
        "generated_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "library": stats,
        "duplicates": {
            "groups": len(duplicates),
            "files": sum(max(0, len(g.files) - 1) for g in duplicates),
            "reclaimable_gb": round(
                sum(
                    int(item.get("size_bytes") or 0)
                    for group in duplicates
                    for item in group.files
                    if item["id"] != group.keeper
                )
                / 1024**3,
                3,
            ),
        },
        "sync_runs": sync_runs,
        "downloads": downloads,
        "top_candidates": candidates,
        "spotify": {
            "last_sync": db.get_setting("spotify.last_sync_at"),
            "last_import": json.loads(db.get_setting("spotify.last_import") or "{}"),
        },
    }


def render_markdown(payload: dict[str, Any]) -> str:
    library = payload["library"]
    lines = [
        "# FiiO Library Report",
        "",
        f"Generated: {payload['generated_at']}",
        "",
        "## Library",
        "",
        f"- Albums: **{library['albums']}**",
        f"- Tracks: **{library['tracks']}**",
        f"- Files indexed: **{library['files']}**",
        f"- Validated files: **{library['validated']}**",
        f"- Verified FLAC: **{library['verified_flac']}**",
        f"- Total size: **{library['size_gb']} GB**",
        f"- Transcode suspects: **{library['transcode_suspect']}**",
        "",
        "### By format",
        "",
        "| Codec | Files | Size (GB) |",
        "|---|---:|---:|",
    ]
    for item in library.get("by_format", []):
        size_gb = round((item.get("size_bytes") or 0) / 1024**3, 3)
        lines.append(f"| {item['codec']} | {item['files']} | {size_gb} |")
    lines += ["", "### By genre", "", "| Genre | Files |", "|---|---:|"]
    for item in library.get("by_genre", [])[:30]:
        lines.append(f"| {item['genre']} | {item['files']} |")

    duplicates = payload["duplicates"]
    lines += [
        "",
        "## Duplicates",
        "",
        f"- Duplicate groups: **{duplicates['groups']}**",
        f"- Extra files: **{duplicates['files']}**",
        f"- Reclaimable: **{duplicates['reclaimable_gb']} GB**",
        "",
        "No files are removed automatically.",
        "",
        "## Downloads",
    ]
    for key, value in payload["downloads"].items():
        lines.append(f"- {key.replace('_', ' ').title()}: **{value}**")

    lines += ["", "## Sync history", "", "| Run | Status | Dry run | Added | Updated | Skipped |", "|---:|---|---|---:|---:|---:|"]
    for run in payload["sync_runs"]:
        lines.append(
            f"| {run['id']} | {run['status']} | {'yes' if run['dry_run'] else 'no'} | "
            f"{run['added']} | {run['updated']} | {run['skipped']} |"
        )

    lines += ["", "## Top discovery candidates", ""]
    for candidate in payload["top_candidates"]:
        reasons = "; ".join(reason.get("text", "") for reason in candidate.get("reasons", [])[:3])
        lines.append(
            f"- **{candidate['entity_name']}** ({candidate['entity_type']}, score {candidate['score']}) — "
            f"{candidate['status']}. {reasons}"
        )

    lines += ["", "## Spotify", ""]
    spotify = payload["spotify"]
    lines.append(f"- Last sync: **{spotify.get('last_sync') or 'never'}**")
    counts = (spotify.get("last_import") or {}).get("saved_tracks")
    if counts is not None:
        lines.append(f"- Saved tracks imported: {counts}")
    return "\n".join(lines) + "\n"


def write_report(config: Config, db: Database, label: str | None = None) -> dict[str, Path]:
    payload = report_payload(config, db)
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    base = f"report-{label + '-' if label else ''}{stamp}"
    config.reports_dir.mkdir(parents=True, exist_ok=True)
    markdown_path = config.reports_dir / f"{base}.md"
    json_path = config.reports_dir / f"{base}.json"
    markdown_path.write_text(render_markdown(payload), encoding="utf-8")
    json_path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    return {"markdown": markdown_path, "json": json_path}
