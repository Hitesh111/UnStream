"""Automated audio acquisition: download, tag, validate, and organise tracks for Echo Mini."""

from __future__ import annotations

import logging
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

from fiiolib.activity import activity
from fiiolib.audio.device_profile import sanitize_component
from fiiolib.config import Config
from fiiolib.db import Database, repo
from fiiolib.library.organiser import Organiser
from fiiolib.metadata.artwork import prepare_artwork
from fiiolib.metadata.tagger import TrackMetadata, write_tags

logger = logging.getLogger(__name__)


def build_search_queries(artist: str, title: str, album: str | None = None) -> list[str]:
    """Build prioritised search queries for YouTube audio acquisition with fallbacks."""
    queries: list[str] = []

    # 1. Standard format: artist - title audio
    queries.append(f"ytsearch1:{artist} - {title} audio")

    # 2. Flexible format without punctuation or 'audio' suffix
    queries.append(f"ytsearch1:{artist} {title}")
    queries.append(f"ytsearch1:{title} {artist}")

    # 3. Cleaned title without parenthetical or bracketed notes (e.g. (Remastered), [Official Audio])
    clean_title = re.sub(r"[\(\[].*?[\)\]]", "", title).strip()
    clean_title = re.sub(r"\s+", " ", clean_title)
    if clean_title and clean_title != title:
        queries.append(f"ytsearch1:{artist} {clean_title}")
        queries.append(f"ytsearch1:{clean_title} {artist}")

    # 4. If title has interior periods/dots (e.g. romanized pinyin like uan1h.ing2y.uang1g.in2l.)
    if "." in title and len(title) > 3:
        clean_dots = re.sub(r"\s+", " ", title.replace(".", " ")).strip()
        queries.append(f"ytsearch1:{artist} {clean_dots}")
        queries.append(f"ytsearch1:{clean_dots}")

    # 5. If album is available and distinct
    if album and album.strip():
        queries.append(f"ytsearch1:{artist} {title} {album}")

    # 6. Fallback: title alone
    queries.append(f"ytsearch1:{title}")

    # Deduplicate while preserving order
    unique: list[str] = []
    for q in queries:
        if q not in unique:
            unique.append(q)
    return unique


def download_track(config: Config, db: Database, track_id: int) -> dict[str, Any]:
    """Download a single track by database track_id, tag it, validate, and organise."""
    row = db.one(
        "SELECT t.id, t.title, a.name AS artist_name, al.title AS album_title, "
        "t.track_number, t.disc_number, al.release_year, al.cover_url, "
        "f.id AS file_id, f.path AS existing_path, f.validated "
        "FROM tracks t "
        "JOIN artists a ON a.id = t.artist_id "
        "JOIN albums al ON al.id = t.album_id "
        "LEFT JOIN files f ON f.track_id = t.id "
        "WHERE t.id = ?",
        (track_id,),
    )
    if not row:
        err = f"Track {track_id} not found in database"
        activity.log(level="error", category="download", message=err, track_id=track_id, error=err)
        return {"ok": False, "error": err}

    title = row["title"]
    artist = row["artist_name"]
    album = row["album_title"]
    track_num = row["track_number"]
    disc_num = row["disc_number"] or 1
    year = str(row["release_year"]) if row["release_year"] else None
    cover_url = row["cover_url"]

    # If already downloaded and verified on disk, return existing
    existing_path = row["existing_path"]
    if existing_path and Path(existing_path).exists() and row["validated"] == 1:
        activity.log(
            level="info",
            category="download",
            message=f"Track #{track_id} '{title}' is already present and verified on disk (skipped)",
            track_id=track_id,
            track_title=title,
            artist=artist,
        )
        return {
            "ok": True,
            "track_id": track_id,
            "already_downloaded": True,
            "path": existing_path,
            "title": title,
            "artist": artist,
        }

    activity.log(
        level="info",
        category="download",
        message=f"Searching audio stream for '{title}' by {artist}…",
        track_id=track_id,
        track_title=title,
        artist=artist,
    )

    staging_dir = config.root / "staging"
    staging_dir.mkdir(parents=True, exist_ok=True)
    staging_output = staging_dir / f"dl_track_{track_id}.flac"

    queries = build_search_queries(artist, title, album)
    download_success = False
    last_err = ""

    for q_idx, search_query in enumerate(queries, 1):
        staging_output.unlink(missing_ok=True)
        for old in staging_dir.glob(f"dl_track_{track_id}*"):
            old.unlink(missing_ok=True)

        if q_idx > 1:
            logger.info("Retrying track #%d with fallback search: '%s'", track_id, search_query)
            activity.log(
                level="info",
                category="download",
                message=f"Retrying '{title}' with search variation: {search_query[10:]}",
                track_id=track_id,
            )

        cmd = [
            sys.executable,
            "-m",
            "yt_dlp",
            "--extract-audio",
            "--audio-format",
            "flac",
            "--audio-quality",
            "0",
            "--postprocessor-args",
            "ffmpeg:-ar 44100 -ac 2 -af asetnsamples=n=4096:p=0 -sample_fmt s16",
            "--default-search",
            "ytsearch",
            "--max-downloads",
            "1",
            "--no-playlist",
            "--no-warnings",
            "-o",
            str(staging_output),
            search_query,
        ]

        proc = subprocess.run(cmd, capture_output=True, text=True)

        if staging_output.exists():
            download_success = True
            break

        candidates = list(staging_dir.glob(f"dl_track_{track_id}*"))
        if candidates and candidates[0].exists():
            candidates[0].replace(staging_output)
            download_success = True
            break

        err_out = proc.stderr.strip() or proc.stdout.strip()
        if proc.returncode not in (0, 101) and err_out:
            last_err = err_out[:200]
        else:
            last_err = "No matching audio stream found on YouTube"

    if not download_success:
        err = last_err or "Downloaded audio stream file was not created"
        logger.error("Download failed for track %d: %s", track_id, err)
        activity.log(
            level="error",
            category="download",
            message=f"✗ Audio download failed for '{title}' by {artist}: {err[:140]}",
            track_id=track_id,
            track_title=title,
            artist=artist,
            error=err[:200],
        )
        return {"ok": False, "error": f"Download failed: {err[:200]}"}

    activity.log(
        level="info",
        category="transcode",
        message=f"Transcoding '{title}' to 44.1kHz FLAC stereo & embedding metadata…",
        track_id=track_id,
        track_title=title,
        artist=artist,
    )

    # Fetch and prepare baseline JPEG artwork for Echo Mini
    art_bytes = None
    if cover_url:
        try:
            import httpx

            resp = httpx.get(cover_url, timeout=10.0, follow_redirects=True)
            if resp.status_code == 200:
                art_bytes, _ = prepare_artwork(resp.content, target_px=500, ffmpeg_path=config.audio.ffmpeg_path)
        except Exception as exc:
            logger.warning("Could not fetch cover art for track %d: %s", track_id, exc)

    # Write clean Vorbis tags and embedded artwork
    meta = TrackMetadata(
        title=title,
        artist=artist,
        album=album,
        track_number=track_num,
        disc_number=disc_num,
        year=year,
    )
    try:
        write_tags(staging_output, meta, artwork=art_bytes)
    except Exception as exc:
        logger.warning("Tag writing warning for track %d: %s", track_id, exc)

    # Organise into deterministic library folder and register in SQLite
    organiser = Organiser(config, db)
    org_res = organiser.organise(staging_output, track_id=track_id)

    if org_res.action in ("move", "already_present", "copied"):
        logger.info("Successfully organised track #%d -> %s", track_id, org_res.destination)
        activity.log(
            level="success",
            category="download",
            message=f"✓ Ready: '{title}' by {artist} (44.1kHz FLAC / Echo Mini verified)",
            track_id=track_id,
            track_title=title,
            artist=artist,
        )
        return {
            "ok": True,
            "track_id": track_id,
            "title": title,
            "artist": artist,
            "album": album,
            "path": org_res.destination,
        }

    fail_msg = org_res.error or "; ".join(org_res.notes) or "Organisation failed"
    activity.log(
        level="error",
        category="validate",
        message=f"✗ Validation/organisation failed for '{title}': {fail_msg}",
        track_id=track_id,
        track_title=title,
        artist=artist,
        error=fail_msg,
    )
    return {
        "ok": False,
        "track_id": track_id,
        "error": fail_msg,
    }


def download_playlist(
    config: Config, db: Database, playlist_id: int, max_tracks: int = 50
) -> dict[str, Any]:
    """Download tracks from a playlist."""
    rows = db.query(
        "SELECT pt.track_id, t.title, a.name AS artist_name "
        "FROM playlist_tracks pt "
        "JOIN tracks t ON t.id = pt.track_id "
        "JOIN artists a ON a.id = t.artist_id "
        "WHERE pt.playlist_id = ? "
        "ORDER BY pt.position ASC LIMIT ?",
        (playlist_id, max_tracks),
    )
    total = len(rows)
    activity.start_operation(
        action="download",
        description=f"Downloading playlist #{playlist_id}",
        total=total,
    )
    downloaded = 0
    skipped = 0
    errors: list[str] = []

    for idx, row in enumerate(rows, 1):
        tid = int(row["track_id"])
        activity.update_progress(
            current=idx,
            total=total,
            current_item=f"{row['title']} — {row['artist_name']}",
        )
        res = download_track(config, db, tid)
        if res.get("ok"):
            if res.get("already_downloaded"):
                skipped += 1
            else:
                downloaded += 1
        else:
            errors.append(f"Track {tid} ({row['title']}): {res.get('error')}")

    # Generate/update the .m3u8 playlist file for Echo Mini
    m3u_path = export_playlist_m3u(config, db, playlist_id)
    if m3u_path:
        activity.log(
            level="info",
            category="playlist",
            message=f"Exported M3U8 playlist for Echo Mini: '{m3u_path.name}'",
        )

    activity.finish_operation(
        summary=f"Playlist #{playlist_id} finished: {downloaded} downloaded, {skipped} skipped, {len(errors)} failed"
    )
    return {
        "ok": True,
        "playlist_id": playlist_id,
        "total": total,
        "downloaded": downloaded,
        "skipped": skipped,
        "errors": errors,
        "m3u_path": str(m3u_path) if m3u_path else None,
    }


def export_playlist_m3u(
    config: Config, db: Database, playlist_id: int, target_dir: Path | None = None
) -> Path | None:
    """Generate an Echo Mini compliant UTF-8 .m3u8 playlist file."""
    pl_row = db.one("SELECT id, name FROM playlists WHERE id = ?", (playlist_id,))
    if not pl_row:
        return None
    raw_name = pl_row["name"]
    name = sanitize_component(raw_name)
    if not target_dir:
        target_dir = config.root / "playlists"
    target_dir.mkdir(parents=True, exist_ok=True)
    m3u_path = target_dir / f"{name}.m3u8"

    tracks = db.query(
        "SELECT t.title, a.name AS artist_name, t.duration_ms, f.path "
        "FROM playlist_tracks pt "
        "JOIN tracks t ON t.id = pt.track_id "
        "JOIN artists a ON a.id = t.artist_id "
        "LEFT JOIN files f ON f.track_id = t.id AND f.validated = 1 "
        "WHERE pt.playlist_id = ? "
        "ORDER BY pt.position ASC",
        (playlist_id,),
    )
    lines = ["#EXTM3U", f"#PLAYLIST:{raw_name}"]
    matched = 0
    music_dir = config.music_dir.resolve()
    for t in tracks:
        file_path_str = t["path"]
        if not file_path_str:
            continue
        fp = Path(file_path_str)
        if not fp.exists():
            continue
        dur_sec = (t["duration_ms"] or 0) // 1000
        lines.append(f"#EXTINF:{dur_sec},{t['artist_name']} - {t['title']}")
        try:
            rel_within_music = fp.resolve().relative_to(music_dir)
            lines.append(f"../Music/{rel_within_music.as_posix()}")
        except ValueError:
            try:
                rel = os.path.relpath(file_path_str, target_dir)
                lines.append(rel)
            except ValueError:
                lines.append(file_path_str)
        matched += 1

    if matched == 0:
        # Don't create empty playlist files
        return None

    m3u_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return m3u_path


def export_saved_tracks_m3u(
    config: Config, db: Database, target_dir: Path | None = None
) -> Path | None:
    """Generate an Echo Mini compliant UTF-8 .m3u8 playlist file for Spotify Liked Songs."""
    if not target_dir:
        target_dir = config.root / "playlists"
    target_dir.mkdir(parents=True, exist_ok=True)
    m3u_path = target_dir / "Liked Songs.m3u8"

    tracks = db.query(
        "SELECT t.title, a.name AS artist_name, t.duration_ms, f.path "
        "FROM spotify_saved_tracks st "
        "JOIN tracks t ON t.id = st.track_id "
        "JOIN artists a ON a.id = t.artist_id "
        "LEFT JOIN files f ON f.track_id = t.id AND f.validated = 1 "
        "ORDER BY st.added_at DESC",
    )
    lines = ["#EXTM3U", "#PLAYLIST:Liked Songs"]
    matched = 0
    music_dir = config.music_dir.resolve()
    for t in tracks:
        file_path_str = t["path"]
        if not file_path_str:
            continue
        fp = Path(file_path_str)
        if not fp.exists():
            continue
        dur_sec = (t["duration_ms"] or 0) // 1000
        lines.append(f"#EXTINF:{dur_sec},{t['artist_name']} - {t['title']}")
        try:
            rel_within_music = fp.resolve().relative_to(music_dir)
            lines.append(f"../Music/{rel_within_music.as_posix()}")
        except ValueError:
            try:
                rel = os.path.relpath(file_path_str, target_dir)
                lines.append(rel)
            except ValueError:
                lines.append(file_path_str)
        matched += 1

    if matched == 0:
        return None

    m3u_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return m3u_path


def export_all_playlists(config: Config, db: Database, target_dir: Path | None = None) -> list[str]:
    """Export all playlists (including Liked Songs) that contain at least one downloaded audio track."""
    rows = db.query("SELECT DISTINCT playlist_id FROM playlist_tracks")
    exported = []
    for r in rows:
        path = export_playlist_m3u(config, db, int(r["playlist_id"]), target_dir=target_dir)
        if path:
            exported.append(str(path))
    saved_path = export_saved_tracks_m3u(config, db, target_dir=target_dir)
    if saved_path:
        exported.append(str(saved_path))
    return exported


def download_playlists_batch(
    config: Config, db: Database, playlist_ids: list[int], max_tracks_per_playlist: int = 50
) -> dict[str, Any]:
    """Download multiple selected playlists sequentially with live progress and M3U export."""
    total_pls = len(playlist_ids)
    activity.start_operation(
        action="download",
        description=f"Batch downloading {total_pls} playlists",
        total=total_pls,
    )
    activity.log(
        level="info",
        category="playlist",
        message=f"Starting batch acquisition of {total_pls} playlists…",
    )

    total_downloaded = 0
    total_skipped = 0
    all_errors = []
    playlist_summaries = []

    for idx, pid in enumerate(playlist_ids, 1):
        pl_row = db.one("SELECT name FROM playlists WHERE id = ?", (pid,))
        pl_name = pl_row["name"] if pl_row else f"Playlist #{pid}"
        activity.update_progress(
            current=idx,
            total=total_pls,
            current_item=f"Playlist ({idx}/{total_pls}): {pl_name}",
        )
        activity.log(
            level="info",
            category="playlist",
            message=f"Processing playlist [{idx}/{total_pls}]: '{pl_name}'",
        )
        res = download_playlist(config, db, pid, max_tracks=max_tracks_per_playlist)
        total_downloaded += res["downloaded"]
        total_skipped += res["skipped"]
        all_errors.extend(res["errors"])
        playlist_summaries.append({
            "playlist_id": pid,
            "name": pl_name,
            "downloaded": res["downloaded"],
            "skipped": res["skipped"],
            "errors": len(res["errors"]),
        })

    activity.finish_operation(
        summary=f"Batch complete ({total_pls} playlists): {total_downloaded} tracks downloaded, {total_skipped} skipped, {len(all_errors)} failed"
    )
    activity.log(
        level="success",
        category="playlist",
        message=f"✓ Batch acquisition complete: {total_downloaded} tracks downloaded across {total_pls} playlists.",
    )
    return {
        "ok": True,
        "playlists_count": total_pls,
        "downloaded": total_downloaded,
        "skipped": total_skipped,
        "errors": all_errors,
        "summaries": playlist_summaries,
    }


def download_all_playlists(
    config: Config, db: Database, max_tracks_per_playlist: int = 50
) -> dict[str, Any]:
    """Download all playlists that have indexed tracks in database."""
    rows = db.query(
        "SELECT p.id FROM playlists p "
        "JOIN playlist_tracks pt ON pt.playlist_id = p.id "
        "GROUP BY p.id "
        "HAVING COUNT(pt.track_id) > 0 "
        "ORDER BY (CASE WHEN p.owner_name = 'Hitesh' THEN 0 ELSE 1 END), p.name ASC"
    )
    pids = [int(r["id"]) for r in rows]
    if not pids:
        return {"ok": True, "count": 0, "downloaded": 0, "skipped": 0, "errors": []}
    return download_playlists_batch(config, db, pids, max_tracks_per_playlist=max_tracks_per_playlist)


def download_saved_tracks(config: Config, db: Database, limit: int | None = None) -> dict[str, Any]:
    """Download user saved (Liked) tracks. If limit is None or <= 0, downloads all liked tracks."""
    if limit and limit > 0:
        rows = db.query(
            "SELECT st.track_id, t.title, a.name AS artist_name "
            "FROM spotify_saved_tracks st "
            "JOIN tracks t ON t.id = st.track_id "
            "JOIN artists a ON a.id = t.artist_id "
            "ORDER BY st.added_at DESC LIMIT ?",
            (limit,),
        )
    else:
        rows = db.query(
            "SELECT st.track_id, t.title, a.name AS artist_name "
            "FROM spotify_saved_tracks st "
            "JOIN tracks t ON t.id = st.track_id "
            "JOIN artists a ON a.id = t.artist_id "
            "ORDER BY st.added_at DESC",
        )
    total = len(rows)
    activity.start_operation(
        action="download",
        description=f"Downloading {total} Liked Songs",
        total=total,
    )
    activity.log(
        level="info",
        category="download",
        message=f"Starting download of {total} Liked Songs…",
    )
    downloaded = 0
    skipped = 0
    errors: list[str] = []

    for idx, row in enumerate(rows, 1):
        tid = int(row["track_id"])
        activity.update_progress(
            current=idx,
            total=total,
            current_item=f"({idx}/{total}) {row['title']} — {row['artist_name']}",
        )
        res = download_track(config, db, tid)
        if res.get("ok"):
            if res.get("already_downloaded"):
                skipped += 1
            else:
                downloaded += 1
        else:
            errors.append(f"Track {tid} ({row['title']}): {res.get('error')}")

    export_saved_tracks_m3u(config, db)

    activity.finish_operation(
        summary=f"Liked songs complete: {downloaded} downloaded, {skipped} skipped, {len(errors)} failed"
    )
    activity.log(
        level="success",
        category="download",
        message=f"✓ Liked songs complete: {downloaded} downloaded, {skipped} skipped, {len(errors)} failed",
    )
    return {
        "ok": True,
        "total": total,
        "downloaded": downloaded,
        "skipped": skipped,
        "errors": errors,
    }


def download_tracks_batch(config: Config, db: Database, track_ids: list[int]) -> dict[str, Any]:
    """Download multiple selected tracks by IDs."""
    total = len(track_ids)
    if not total:
        return {"ok": True, "total": 0, "downloaded": 0, "skipped": 0, "errors": []}
    activity.start_operation(
        action="download",
        description=f"Downloading {total} selected tracks",
        total=total,
    )
    activity.log(
        level="info",
        category="download",
        message=f"Starting batch download of {total} selected tracks…",
    )
    downloaded = 0
    skipped = 0
    errors: list[str] = []

    for idx, tid in enumerate(track_ids, 1):
        t_row = db.one(
            "SELECT t.title, a.name AS artist_name FROM tracks t JOIN artists a ON a.id = t.artist_id WHERE t.id = ?",
            (tid,),
        )
        title = t_row["title"] if t_row else f"Track #{tid}"
        artist = t_row["artist_name"] if t_row else "Unknown"
        activity.update_progress(
            current=idx,
            total=total,
            current_item=f"({idx}/{total}) {title} — {artist}",
        )
        res = download_track(config, db, tid)
        if res.get("ok"):
            if res.get("already_downloaded"):
                skipped += 1
            else:
                downloaded += 1
        else:
            errors.append(f"Track {tid} ({title}): {res.get('error')}")

    export_all_playlists(config, db)

    activity.finish_operation(
        summary=f"Batch track download complete: {downloaded} downloaded, {skipped} skipped, {len(errors)} failed"
    )
    activity.log(
        level="success",
        category="download",
        message=f"✓ Batch track download complete: {downloaded} downloaded, {skipped} skipped, {len(errors)} failed",
    )
    return {
        "ok": True,
        "total": total,
        "downloaded": downloaded,
        "skipped": skipped,
        "errors": errors,
    }
