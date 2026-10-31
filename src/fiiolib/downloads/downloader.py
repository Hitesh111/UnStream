"""Automated audio acquisition: download, tag, validate, and organise tracks for Echo Mini."""

from __future__ import annotations

import logging
import subprocess
import sys
from pathlib import Path
from typing import Any

from fiiolib.activity import activity
from fiiolib.config import Config
from fiiolib.db import Database, repo
from fiiolib.library.organiser import Organiser
from fiiolib.metadata.artwork import prepare_artwork
from fiiolib.metadata.tagger import TrackMetadata, write_tags

logger = logging.getLogger(__name__)


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
    staging_output.unlink(missing_ok=True)

    search_query = f"ytsearch1:{artist} - {title} audio"
    logger.info("Downloading track #%d: '%s' by '%s'", track_id, title, artist)

    # Invoke yt-dlp to extract high quality audio and convert to 44.1kHz stereo FLAC
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
        "ffmpeg:-ar 44100 -ac 2",
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
    # yt-dlp returns 101 when stopping due to --max-downloads 1, which is expected
    if not staging_output.exists() and proc.returncode not in (0, 101):
        err = proc.stderr.strip() or proc.stdout.strip() or f"Process returned {proc.returncode}"
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

    if not staging_output.exists():
        # Sometimes yt-dlp appends .flac
        candidates = list(staging_dir.glob(f"dl_track_{track_id}*"))
        if candidates and candidates[0].exists():
            candidates[0].replace(staging_output)
        else:
            err = "Downloaded audio stream file was not created"
            activity.log(
                level="error",
                category="download",
                message=f"✗ Stream creation failed for '{title}': no output file",
                track_id=track_id,
                track_title=title,
                artist=artist,
                error=err,
            )
            return {"ok": False, "error": err}

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
    }


def download_saved_tracks(config: Config, db: Database, limit: int = 25) -> dict[str, Any]:
    """Download top user saved (Liked) tracks."""
    rows = db.query(
        "SELECT st.track_id, t.title, a.name AS artist_name "
        "FROM spotify_saved_tracks st "
        "JOIN tracks t ON t.id = st.track_id "
        "JOIN artists a ON a.id = t.artist_id "
        "ORDER BY st.added_at DESC LIMIT ?",
        (limit,),
    )
    total = len(rows)
    activity.start_operation(
        action="download",
        description=f"Downloading {total} Liked Songs",
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

    activity.finish_operation(
        summary=f"Liked songs complete: {downloaded} downloaded, {skipped} skipped, {len(errors)} failed"
    )
    return {
        "ok": True,
        "total": total,
        "downloaded": downloaded,
        "skipped": skipped,
        "errors": errors,
    }
