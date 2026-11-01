"""Automated audio acquisition: download, tag, validate, and organise tracks for Echo Mini."""

from __future__ import annotations

import logging
import os
import re
import shutil
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


def clean_volume_junk(volume_path: Path) -> int:
    """Purge macOS AppleDouble ._* resource forks and .DS_Store from target volume."""
    cleaned = 0
    if not volume_path.exists():
        return 0
    if sys.platform == "darwin" and shutil.which("dot_clean"):
        try:
            subprocess.run(["dot_clean", "-m", str(volume_path)], capture_output=True, check=False, timeout=20)
        except Exception:
            pass
    for root, dirs, files in os.walk(volume_path):
        for f in files:
            if f.startswith("._") or f in (".DS_Store", "Thumbs.db"):
                fp = os.path.join(root, f)
                try:
                    os.remove(fp)
                    cleaned += 1
                except Exception:
                    pass
    return cleaned


def _write_m3u_bundle(
    target_dir: Path,
    name: str,
    tracks: list[dict[str, Any]],
    music_dir: Path,
) -> Path | None:
    """Write compliant CRLF UTF-8 .m3u and .m3u8 files with relative and root path variations."""
    if not tracks:
        return None
    target_dir.mkdir(parents=True, exist_ok=True)
    m3u_path = target_dir / f"{name}.m3u"
    m3u8_path = target_dir / f"{name}.m3u8"

    lines_rel: list[str] = ["#EXTM3U"]
    lines_root: list[str] = ["#EXTM3U"]
    matched = 0

    for t in tracks:
        file_path_str = t["path"]
        if not file_path_str:
            continue
        fp = Path(file_path_str)
        if not fp.exists():
            continue
        dur = t["duration_ms"] if ("duration_ms" in t.keys() and t["duration_ms"]) else 0
        dur_sec = dur // 1000
        extinf = f"#EXTINF:{dur_sec},{t['artist_name']} - {t['title']}"
        lines_rel.append(extinf)
        lines_root.append(extinf)

        try:
            rel_within_music = fp.resolve().relative_to(music_dir.resolve())
            rel_posix = rel_within_music.as_posix()
            lines_rel.append(f"../Music/{rel_posix}")
            lines_root.append(f"Music/{rel_posix}")
        except ValueError:
            try:
                rel = os.path.relpath(file_path_str, target_dir)
                lines_rel.append(rel)
                lines_root.append(file_path_str)
            except ValueError:
                lines_rel.append(file_path_str)
                lines_root.append(file_path_str)
        matched += 1

    if matched == 0:
        return None

    # DOS CRLF is required by embedded FAT32 players (Ingenic/Actions/Rockchip)
    content_rel = "\r\n".join(lines_rel) + "\r\n"
    content_root = "\r\n".join(lines_root) + "\r\n"

    # Write both .m3u and .m3u8 in target_dir
    m3u_path.write_bytes(content_rel.encode("utf-8"))
    m3u8_path.write_bytes(content_rel.encode("utf-8"))

    # Also place root copies if target_dir is a subfolder on a volume/drive
    parent = target_dir.parent
    if parent and parent != target_dir and parent.exists():
        if target_dir.name.lower() in ("playlists", "playlist"):
            try:
                (parent / f"{name}.m3u").write_bytes(content_root.encode("utf-8"))
                (parent / f"{name}.m3u8").write_bytes(content_root.encode("utf-8"))
            except Exception:
                pass

    return m3u_path


def export_playlist_m3u(
    config: Config, db: Database, playlist_id: int, target_dir: Path | None = None
) -> Path | None:
    """Generate compliant UTF-8 .m3u and .m3u8 playlist files for Echo Mini / FiiO players."""
    pl_row = db.one("SELECT id, name FROM playlists WHERE id = ?", (playlist_id,))
    if not pl_row:
        return None
    raw_name = pl_row["name"]
    name = sanitize_component(raw_name)
    if not target_dir:
        target_dir = config.root / "playlists"

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
    return _write_m3u_bundle(target_dir, name, tracks, config.music_dir)


def export_saved_tracks_m3u(
    config: Config, db: Database, target_dir: Path | None = None
) -> Path | None:
    """Generate compliant UTF-8 .m3u and .m3u8 playlist files for Liked Songs."""
    if not target_dir:
        target_dir = config.root / "playlists"

    tracks = db.query(
        "SELECT t.title, a.name AS artist_name, t.duration_ms, f.path "
        "FROM spotify_saved_tracks st "
        "JOIN tracks t ON t.id = st.track_id "
        "JOIN artists a ON a.id = t.artist_id "
        "LEFT JOIN files f ON f.track_id = t.id AND f.validated = 1 "
        "ORDER BY st.added_at DESC",
    )
    return _write_m3u_bundle(target_dir, "Liked Songs", tracks, config.music_dir)


def export_all_playlists(config: Config, db: Database, target_dir: Path | None = None) -> list[str]:
    """Export all playlists (including Liked Songs) to local library and active card target."""
    rows = db.query("SELECT DISTINCT playlist_id FROM playlist_tracks")
    targets: list[Path] = []

    if target_dir:
        targets.append(target_dir)
    else:
        # Default target: local library playlists directory
        local_pl = config.root / "playlists"
        targets.append(local_pl)
        # Check if an active microSD card or custom sync folder is configured/connected
        from fiiolib.device.detect import DeviceLocator
        locator = DeviceLocator(config)
        dev = locator.current()
        if dev and dev.mount_point:
            mp = Path(dev.mount_point)
            if mp.exists():
                card_pl = mp / "Playlists"
                targets.append(card_pl)

    exported = []
    for tgt in targets:
        for r in rows:
            path = export_playlist_m3u(config, db, int(r["playlist_id"]), target_dir=tgt)
            if path and str(path) not in exported:
                exported.append(str(path))
        saved_path = export_saved_tracks_m3u(config, db, target_dir=tgt)
        if saved_path and str(saved_path) not in exported:
            exported.append(str(saved_path))
        # Purge any AppleDouble resource forks on destination
        clean_volume_junk(tgt)

    return exported


def export_folder_playlists(
    config: Config,
    db: Database,
    target_dir: Path | None = None,
    playlist_ids: list[int] | None = None,
) -> dict[str, Any]:
    """Export playlists as physical track folders (EchoList mode) for guaranteed playback on any DAP firmware."""
    if target_dir is None:
        from fiiolib.device.detect import DeviceLocator
        locator = DeviceLocator(config)
        dev = locator.current()
        if dev and dev.mount_point:
            mp = Path(dev.mount_point)
            if mp.exists():
                target_dir = mp / "Playlists"
            else:
                target_dir = config.root / "playlists"
        else:
            target_dir = config.root / "playlists"

    target_dir.mkdir(parents=True, exist_ok=True)

    if playlist_ids:
        pl_rows = db.query(
            "SELECT id, name FROM playlists WHERE id IN ({})".format(",".join("?" * len(playlist_ids))),
            tuple(playlist_ids),
        )
    else:
        pl_rows = db.query(
            "SELECT DISTINCT p.id, p.name FROM playlists p "
            "JOIN playlist_tracks pt ON pt.playlist_id = p.id "
            "ORDER BY p.name ASC"
        )

    results = []
    total_copied = 0
    total_skipped = 0

    for pl in pl_rows:
        pl_id = int(pl["id"])
        pl_name = sanitize_component(pl["name"])
        folder = target_dir / pl_name
        folder.mkdir(parents=True, exist_ok=True)

        tracks = db.query(
            "SELECT t.title, a.name AS artist_name, pt.position, f.path "
            "FROM playlist_tracks pt "
            "JOIN tracks t ON t.id = pt.track_id "
            "JOIN artists a ON a.id = t.artist_id "
            "LEFT JOIN files f ON f.track_id = t.id AND f.validated = 1 "
            "WHERE pt.playlist_id = ? "
            "ORDER BY pt.position ASC",
            (pl_id,),
        )
        copied = 0
        skipped = 0
        for idx, t in enumerate(tracks, 1):
            src_str = t["path"]
            if not src_str:
                continue
            src = Path(src_str)
            if not src.exists():
                continue
            ext = src.suffix or ".flac"
            safe_title = sanitize_component(t["title"])
            safe_artist = sanitize_component(t["artist_name"])
            dest = folder / f"{idx:02d} - {safe_artist} - {safe_title}{ext}"
            if dest.exists() and dest.stat().st_size == src.stat().st_size:
                skipped += 1
            else:
                shutil.copy2(src, dest)
                copied += 1
        total_copied += copied
        total_skipped += skipped
        results.append({"name": pl["name"], "folder": str(folder), "copied": copied, "skipped": skipped, "total": len(tracks)})

    # Also handle Liked Songs
    liked_tracks = db.query(
        "SELECT t.title, a.name AS artist_name, f.path "
        "FROM spotify_saved_tracks st "
        "JOIN tracks t ON t.id = st.track_id "
        "JOIN artists a ON a.id = t.artist_id "
        "LEFT JOIN files f ON f.track_id = t.id AND f.validated = 1 "
        "ORDER BY st.added_at DESC",
    )
    if liked_tracks:
        liked_folder = target_dir / "Liked Songs"
        liked_folder.mkdir(parents=True, exist_ok=True)
        c_liked = 0
        s_liked = 0
        for idx, t in enumerate(liked_tracks, 1):
            src_str = t["path"]
            if not src_str:
                continue
            src = Path(src_str)
            if not src.exists():
                continue
            ext = src.suffix or ".flac"
            safe_title = sanitize_component(t["title"])
            safe_artist = sanitize_component(t["artist_name"])
            dest = liked_folder / f"{idx:02d} - {safe_artist} - {safe_title}{ext}"
            if dest.exists() and dest.stat().st_size == src.stat().st_size:
                s_liked += 1
            else:
                shutil.copy2(src, dest)
                c_liked += 1
        total_copied += c_liked
        total_skipped += s_liked
        results.append({"name": "Liked Songs", "folder": str(liked_folder), "copied": c_liked, "skipped": s_liked, "total": len(liked_tracks)})

    clean_volume_junk(target_dir)

    return {
        "ok": True,
        "total_copied": total_copied,
        "total_skipped": total_skipped,
        "playlists": results,
    }


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
