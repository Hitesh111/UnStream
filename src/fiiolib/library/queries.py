"""Read models shared by the library workflows."""

from __future__ import annotations

from typing import Any

from fiiolib.db import Database


def track_context(db: Database, track_id: int) -> dict[str, Any] | None:
    row = db.one(
        "SELECT t.id, t.title, t.track_number, t.disc_number, t.duration_ms, t.isrc, "
        "t.spotify_id, t.source_url, ar.name AS artist_name, ar.id AS artist_id, "
        "al.title AS album_title, al.id AS album_id, al.release_year, al.release_date, "
        "al.cover_url, l.name AS label_name "
        "FROM tracks t LEFT JOIN artists ar ON ar.id = t.artist_id "
        "LEFT JOIN albums al ON al.id = t.album_id "
        "LEFT JOIN labels l ON l.id = al.label_id WHERE t.id = ?",
        (track_id,),
    )
    if row is None:
        return None
    context = dict(row)
    context["genres"] = track_genres(db, track_id)
    return context


def track_genres(db: Database, track_id: int) -> list[str]:
    rows = db.query(
        "SELECT g.name, tg.weight FROM track_genres tg JOIN genres g ON g.id = tg.genre_id "
        "WHERE tg.track_id = ? "
        "UNION "
        "SELECT g.name, ag.weight FROM album_genres ag JOIN genres g ON g.id = ag.genre_id "
        "JOIN tracks t ON t.album_id = ag.album_id WHERE t.id = ? "
        "UNION "
        "SELECT g.name, rg.weight * 0.7 AS weight FROM artist_genres rg JOIN genres g ON g.id = rg.genre_id "
        "JOIN track_artists ta ON ta.artist_id = rg.artist_id WHERE ta.track_id = ? "
        "ORDER BY weight DESC",
        (track_id, track_id, track_id),
    )
    seen: list[str] = []
    for row in rows:
        if row["name"] not in seen:
            seen.append(row["name"])
    return seen


def library_stats(db: Database) -> dict[str, Any]:
    total_files = int(db.scalar("SELECT COUNT(*) FROM files") or 0)
    validated = int(db.scalar("SELECT COUNT(*) FROM files WHERE validated = 1") or 0)
    total_bytes = int(db.scalar("SELECT COALESCE(SUM(size_bytes), 0) FROM files") or 0)
    lossless = int(
        db.scalar("SELECT COUNT(*) FROM files WHERE codec = 'flac' AND validated = 1") or 0
    )
    suspect = int(db.scalar("SELECT COUNT(*) FROM files WHERE transcode_suspect = 1") or 0)
    by_genre = [
        dict(row)
        for row in db.query(
            "SELECT COALESCE(g.name, 'Unsorted') AS genre, COUNT(*) AS files "
            "FROM files f LEFT JOIN tracks t ON t.id = f.track_id "
            "LEFT JOIN track_genres tg ON tg.track_id = t.id "
            "LEFT JOIN genres g ON g.id = tg.genre_id GROUP BY genre ORDER BY files DESC"
        )
    ]
    by_format = [
        dict(row)
        for row in db.query(
            "SELECT COALESCE(codec, 'unknown') AS codec, COUNT(*) AS files, "
            "SUM(size_bytes) AS size_bytes FROM files GROUP BY codec ORDER BY files DESC"
        )
    ]
    return {
        "files": total_files,
        "validated": validated,
        "verified_flac": lossless,
        "transcode_suspect": suspect,
        "size_bytes": total_bytes,
        "size_gb": round(total_bytes / 1024**3, 3),
        "by_genre": by_genre,
        "by_format": by_format,
        "albums": int(db.scalar("SELECT COUNT(DISTINCT album_id) FROM tracks WHERE album_id IS NOT NULL") or 0),
        "tracks": int(db.scalar("SELECT COUNT(*) FROM tracks") or 0),
        "artists": int(db.scalar("SELECT COUNT(*) FROM artists") or 0),
    }


def files_for_collection(
    db: Database,
    genres: list[str] | None = None,
    artist_ids: list[int] | None = None,
    album_ids: list[int] | None = None,
    only_verified: bool = True,
) -> list[dict[str, Any]]:
    clauses = ["f.track_id IS NOT NULL", "f.organised_at IS NOT NULL"]
    params: list[Any] = []
    if only_verified:
        clauses.append("f.validated = 1")
    if genres:
        placeholders = ",".join("?" for _ in genres)
        clauses.append(
            f"t.id IN (SELECT tg.track_id FROM track_genres tg JOIN genres g ON g.id = tg.genre_id "
            f"WHERE g.name IN ({placeholders}))"
        )
        params.extend(genres)
    if artist_ids:
        placeholders = ",".join("?" for _ in artist_ids)
        clauses.append(f"t.artist_id IN ({placeholders})")
        params.extend(artist_ids)
    if album_ids:
        placeholders = ",".join("?" for _ in album_ids)
        clauses.append(f"t.album_id IN ({placeholders})")
        params.extend(album_ids)
    sql = (
        "SELECT f.id, f.path, f.size_bytes, f.sha256, f.codec, f.bit_depth, f.sample_rate, "
        "t.id AS track_id, t.title, t.track_number, t.disc_number, "
        "ar.name AS artist_name, al.title AS album_title "
        "FROM files f JOIN tracks t ON t.id = f.track_id "
        "LEFT JOIN artists ar ON ar.id = t.artist_id "
        "LEFT JOIN albums al ON al.id = t.album_id "
        f"WHERE {' AND '.join(clauses)} ORDER BY ar.name, al.title, t.disc_number, t.track_number"
    )
    return [dict(row) for row in db.query(sql, params)]
