"""Repository helpers: idempotent upserts and lookups shared by all workflows."""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from typing import Any

from fiiolib.db.database import Database, utcnow


def _update(table: str, row_id: int, fields: dict[str, Any], db: Database) -> None:
    if not fields:
        return
    fields = {k: v for k, v in fields.items() if v is not None}
    if not fields:
        return
    assignments = ", ".join(f"{key} = ?" for key in fields)
    db.execute(
        f"UPDATE {table} SET {assignments}, updated_at = ? WHERE id = ?",
        (*fields.values(), utcnow(), row_id),
    )


# --------------------------------------------------------------------- artists
def get_or_create_artist(
    db: Database,
    name: str,
    spotify_id: str | None = None,
    **fields: Any,
) -> int:
    row = None
    if spotify_id:
        row = db.one("SELECT id FROM artists WHERE spotify_id = ?", (spotify_id,))
    if row is None:
        row = db.one("SELECT id FROM artists WHERE name = ? COLLATE NOCASE", (name,))
    if row is not None:
        if spotify_id:
            db.execute(
                "UPDATE artists SET spotify_id = COALESCE(spotify_id, ?) WHERE id = ?",
                (spotify_id, row["id"]),
            )
        _update("artists", int(row["id"]), fields, db)
        return int(row["id"])
    cur = db.execute(
        "INSERT INTO artists(spotify_id, name, sort_name, image_url, musicbrainz_id, source) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (
            spotify_id,
            name,
            fields.get("sort_name"),
            fields.get("image_url"),
            fields.get("musicbrainz_id"),
            fields.get("source", "spotify"),
        ),
    )
    return int(cur.lastrowid)


def find_artist(db: Database, name: str) -> int | None:
    row = db.one("SELECT id FROM artists WHERE name = ? COLLATE NOCASE", (name,))
    return int(row["id"]) if row else None


# ---------------------------------------------------------------------- labels
def get_or_create_label(db: Database, name: str, **fields: Any) -> int:
    row = db.one("SELECT id FROM labels WHERE name = ? COLLATE NOCASE", (name,))
    if row:
        return int(row["id"])
    cur = db.execute(
        "INSERT INTO labels(name, musicbrainz_id, discogs_id) VALUES (?, ?, ?)",
        (name, fields.get("musicbrainz_id"), fields.get("discogs_id")),
    )
    return int(cur.lastrowid)


# ---------------------------------------------------------------------- albums
def get_or_create_album(
    db: Database,
    title: str,
    artist_id: int | None = None,
    spotify_id: str | None = None,
    **fields: Any,
) -> int:
    row = None
    if spotify_id:
        row = db.one("SELECT id FROM albums WHERE spotify_id = ?", (spotify_id,))
    if row is None and artist_id is not None:
        row = db.one(
            "SELECT id FROM albums WHERE title = ? COLLATE NOCASE AND artist_id = ?",
            (title, artist_id),
        )
    if row is not None:
        if spotify_id:
            db.execute(
                "UPDATE albums SET spotify_id = COALESCE(spotify_id, ?) WHERE id = ?",
                (spotify_id, row["id"]),
            )
        _update("albums", int(row["id"]), fields, db)
        return int(row["id"])
    cur = db.execute(
        "INSERT INTO albums("
        "spotify_id, title, artist_id, album_type, release_date, release_year, "
        "label_id, total_tracks, cover_url, musicbrainz_release_group, musicbrainz_release, source"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            spotify_id,
            title,
            artist_id,
            fields.get("album_type"),
            fields.get("release_date"),
            fields.get("release_year"),
            fields.get("label_id"),
            fields.get("total_tracks"),
            fields.get("cover_url"),
            fields.get("musicbrainz_release_group"),
            fields.get("musicbrainz_release"),
            fields.get("source", "spotify"),
        ),
    )
    return int(cur.lastrowid)


# ---------------------------------------------------------------------- tracks
def get_or_create_track(
    db: Database,
    title: str,
    artist_id: int | None = None,
    album_id: int | None = None,
    spotify_id: str | None = None,
    **fields: Any,
) -> int:
    row = None
    if spotify_id:
        row = db.one("SELECT id FROM tracks WHERE spotify_id = ?", (spotify_id,))
    if row is None and fields.get("isrc"):
        row = db.one("SELECT id FROM tracks WHERE isrc = ?", (fields["isrc"],))
    if row is None and album_id is not None:
        row = db.one(
            "SELECT id FROM tracks WHERE title = ? COLLATE NOCASE AND album_id = ? AND "
            "COALESCE(disc_number, 1) = ? AND COALESCE(track_number, 0) = ?",
            (title, album_id, fields.get("disc_number", 1), fields.get("track_number", 0)),
        )
    if row is not None:
        if spotify_id:
            db.execute(
                "UPDATE tracks SET spotify_id = COALESCE(spotify_id, ?) WHERE id = ?",
                (spotify_id, row["id"]),
            )
        _update("tracks", int(row["id"]), fields, db)
        return int(row["id"])
    cur = db.execute(
        "INSERT INTO tracks("
        "spotify_id, isrc, musicbrainz_recording_id, title, artist_id, album_id, "
        "disc_number, track_number, duration_ms, explicit, source_url"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            spotify_id,
            fields.get("isrc"),
            fields.get("musicbrainz_recording_id"),
            title,
            artist_id,
            album_id,
            fields.get("disc_number", 1),
            fields.get("track_number", 0),
            fields.get("duration_ms"),
            1 if fields.get("explicit") else 0,
            fields.get("source_url"),
        ),
    )
    return int(cur.lastrowid)


# ---------------------------------------------------------------------- genres
def link_credits(db: Database, table: str, id_column: str, entity_id: int, artist_ids: Sequence[int]) -> None:
    if table not in {"track_artists", "album_artists"}:
        raise ValueError(f"invalid credit table {table}")
    for position, artist_id in enumerate(dict.fromkeys(artist_ids)):
        db.execute(
            f"INSERT OR IGNORE INTO {table}({id_column}, artist_id, position) VALUES (?, ?, ?)",
            (entity_id, artist_id, position),
        )


def get_or_create_genre(db: Database, name: str, parent_id: int | None = None) -> int:
    name = name.strip()
    row = db.one("SELECT id FROM genres WHERE name = ? COLLATE NOCASE", (name,))
    if row:
        return int(row["id"])
    cur = db.execute("INSERT INTO genres(name, parent_id) VALUES (?, ?)", (name, parent_id))
    return int(cur.lastrowid)


def link_genres(
    db: Database,
    table: str,
    id_column: str,
    entity_id: int,
    genres: Iterable[str],
    weight: float = 1.0,
    source: str = "spotify",
) -> None:
    if table not in {"track_genres", "album_genres", "artist_genres"}:
        raise ValueError(f"invalid genre link table {table}")
    for genre in genres:
        if not genre:
            continue
        genre_id = get_or_create_genre(db, str(genre))
        db.execute(
            f"INSERT INTO {table}({id_column}, genre_id, weight, source) VALUES (?, ?, ?, ?) "
            "ON CONFLICT DO UPDATE SET weight = MAX(weight, excluded.weight)",
            (entity_id, genre_id, weight, source),
        )


# -------------------------------------------------------------------- playlists
def get_or_create_playlist(
    db: Database,
    name: str,
    spotify_id: str | None = None,
    **fields: Any,
) -> int:
    row = None
    if spotify_id:
        row = db.one("SELECT id FROM playlists WHERE spotify_id = ?", (spotify_id,))
    if row is None:
        row = db.one(
            "SELECT id FROM playlists WHERE name = ? COLLATE NOCASE AND "
            "COALESCE(owner_name, '') = COALESCE(?, '')",
            (name, fields.get("owner_name")),
        )
    if row is not None:
        if spotify_id:
            db.execute(
                "UPDATE playlists SET spotify_id = COALESCE(spotify_id, ?) WHERE id = ?",
                (spotify_id, row["id"]),
            )
        _update("playlists", int(row["id"]), fields, db)
        return int(row["id"])
    cur = db.execute(
        "INSERT INTO playlists("
        "spotify_id, name, owner_id, owner_name, description, public, collaborative, "
        "snapshot_id, kind, total_tracks, last_synced_at"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            spotify_id,
            name,
            fields.get("owner_id"),
            fields.get("owner_name"),
            fields.get("description"),
            1 if fields.get("public") else 0,
            1 if fields.get("collaborative") else 0,
            fields.get("snapshot_id"),
            fields.get("kind", "personal"),
            fields.get("total_tracks", 0),
            fields.get("last_synced_at"),
        ),
    )
    return int(cur.lastrowid)


def replace_playlist_tracks(
    db: Database,
    playlist_id: int,
    items: Sequence[tuple[int, int, str | None, str | None]],
) -> None:
    db.execute("DELETE FROM playlist_tracks WHERE playlist_id = ?", (playlist_id,))
    db.executemany(
        "INSERT OR REPLACE INTO playlist_tracks(playlist_id, track_id, position, added_at, added_by) "
        "VALUES (?, ?, ?, ?, ?)",
        [(playlist_id, *item) for item in items],
    )


# ------------------------------------------------------------------ candidates
def upsert_candidate(
    db: Database,
    entity_type: str,
    entity_name: str,
    entity_spotify_id: str | None,
    score: float,
    reasons: list[dict[str, Any]],
    artist_id: int | None = None,
    album_id: int | None = None,
) -> int:
    existing = None
    if entity_spotify_id:
        existing = db.one(
            "SELECT id, status FROM discovery_candidates WHERE entity_type = ? AND entity_spotify_id = ?",
            (entity_type, entity_spotify_id),
        )
    if existing:
        db.execute(
            "UPDATE discovery_candidates SET score = ?, reasons = ?, artist_id = COALESCE(?, artist_id), "
            "album_id = COALESCE(?, album_id), updated_at = ? WHERE id = ?",
            (score, json.dumps(reasons), artist_id, album_id, utcnow(), existing["id"]),
        )
        return int(existing["id"])
    cur = db.execute(
        "INSERT INTO discovery_candidates("
        "entity_type, entity_spotify_id, entity_name, artist_id, album_id, score, reasons, status"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, 'awaiting_approval')",
        (entity_type, entity_spotify_id, entity_name, artist_id, album_id, score, json.dumps(reasons)),
    )
    return int(cur.lastrowid)


def set_candidate_status(db: Database, candidate_id: int, status: str, notes: str | None = None) -> None:
    db.execute(
        "UPDATE discovery_candidates SET status = ?, notes = COALESCE(?, notes), "
        "reviewed_at = CASE WHEN ? IN ('approved','rejected') THEN ? ELSE reviewed_at END, "
        "updated_at = ? WHERE id = ?",
        (status, notes, status, utcnow(), utcnow(), candidate_id),
    )


# --------------------------------------------------------------------- releases
def upsert_release(db: Database, source_id: int, external_id: str, **fields: Any) -> int:
    row = db.one(
        "SELECT id FROM releases WHERE source_id = ? AND external_id = ?", (source_id, external_id)
    )
    payload = {
        "title": fields.get("title", external_id),
        "artist_name": fields.get("artist_name"),
        "album_id": fields.get("album_id"),
        "candidate_id": fields.get("candidate_id"),
        "media_type": fields.get("media_type"),
        "quality": fields.get("quality"),
        "size_bytes": fields.get("size_bytes"),
        "download_url": fields.get("download_url"),
        "licence": fields.get("licence"),
        "licence_url": fields.get("licence_url"),
        "redistribution_allowed": fields.get("redistribution_allowed"),
        "authorisation": fields.get("authorisation", "unknown"),
        "needs_manual_review": 1 if fields.get("needs_manual_review", True) else 0,
        "metadata": json.dumps(fields.get("metadata") or {}, default=str),
    }
    if row:
        assignments = ", ".join(f"{key} = ?" for key in payload)
        db.execute(
            f"UPDATE releases SET {assignments} WHERE id = ?",
            (*payload.values(), row["id"]),
        )
        return int(row["id"])
    columns = ", ".join(payload)
    placeholders = ", ".join("?" for _ in payload)
    cur = db.execute(
        f"INSERT INTO releases(source_id, external_id, {columns}) VALUES (?, ?, {placeholders})",
        (source_id, external_id, *payload.values()),
    )
    return int(cur.lastrowid)


# ------------------------------------------------------------------------ files
def upsert_file(db: Database, path: str, **fields: Any) -> int:
    row = db.one("SELECT id FROM files WHERE path = ?", (path,))
    allowed = {
        "track_id", "size_bytes", "mtime", "sha256", "format", "codec", "bit_depth",
        "sample_rate", "channels", "bitrate", "duration_ms", "validated",
        "validation_notes", "transcode_suspect", "organised_at",
    }
    payload = {k: v for k, v in fields.items() if k in allowed}
    if row:
        if payload:
            assignments = ", ".join(f"{key} = ?" for key in payload)
            db.execute(
                f"UPDATE files SET {assignments}, updated_at = ? WHERE id = ?",
                (*payload.values(), utcnow(), row["id"]),
            )
        return int(row["id"])
    payload.setdefault("size_bytes", 0)
    columns = ", ".join(payload)
    placeholders = ", ".join("?" for _ in payload)
    cur = db.execute(
        f"INSERT INTO files(path, {columns}) VALUES (?, {placeholders})",
        (path, *payload.values()),
    )
    return int(cur.lastrowid)
