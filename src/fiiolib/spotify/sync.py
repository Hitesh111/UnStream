"""Full and incremental Spotify library import."""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from fiiolib.config import Config
from fiiolib.db import Database, repo, utcnow
from fiiolib.spotify.auth import CLIENT_ID_SETTING
from fiiolib.spotify.client import (
    SpotifyClient,
    SpotifyUnavailable,
    normalize_playlist_item,
    primary_artist,
)

logger = logging.getLogger(__name__)

LAST_SYNC_KEY = "spotify.last_sync_at"
IMPORT_SUMMARY_KEY = "spotify.last_import"


@dataclass
class ImportStats:
    saved_tracks: int = 0
    saved_albums: int = 0
    playlists: int = 0
    playlist_items: int = 0
    followed_artists: int = 0
    top_artists: int = 0
    top_tracks: int = 0
    recently_played: int = 0
    artists_created: int = 0
    albums_created: int = 0
    tracks_created: int = 0
    skipped_playlists: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    started_at: str = field(default_factory=utcnow)

    def as_dict(self) -> dict[str, Any]:
        return {
            "saved_tracks": self.saved_tracks,
            "saved_albums": self.saved_albums,
            "playlists": self.playlists,
            "playlist_items": self.playlist_items,
            "followed_artists": self.followed_artists,
            "top_artists": self.top_artists,
            "top_tracks": self.top_tracks,
            "recently_played": self.recently_played,
            "artists_created": self.artists_created,
            "albums_created": self.albums_created,
            "tracks_created": self.tracks_created,
            "skipped_playlists": self.skipped_playlists,
            "warnings": self.warnings,
            "started_at": self.started_at,
            "finished_at": utcnow(),
        }


def _release_year(release_date: str | None) -> int | None:
    if not release_date:
        return None
    try:
        return int(str(release_date)[:4])
    except ValueError:
        return None


def _largest_image(images: Iterable[dict[str, Any]] | None) -> str | None:
    images = list(images or [])
    if not images:
        return None
    return max(images, key=lambda image: (image.get("width") or 0) * (image.get("height") or 0)).get("url")


class SpotifyImporter:
    def __init__(self, config: Config, db: Database, client: SpotifyClient) -> None:
        self.config = config
        self.db = db
        self.client = client
        self.stats = ImportStats()
        self._known_artists: dict[str, int] = {}
        self._known_albums: dict[str, int] = {}
        self._known_tracks: dict[str, int] = {}

    # ---------------------------------------------------------- persistence
    def _artist(self, payload: dict[str, Any]) -> int:
        name = payload.get("name") or "Unknown Artist"
        spotify_id = payload.get("id")
        genres = payload.get("genres") or []
        if spotify_id and spotify_id in self._known_artists:
            artist_id = self._known_artists[spotify_id]
            if genres:
                repo.link_genres(self.db, "artist_genres", "artist_id", artist_id, genres, source="spotify")
            return artist_id
        existing = None
        if spotify_id:
            existing = self.db.one("SELECT id FROM artists WHERE spotify_id = ?", (spotify_id,))
        artist_id = repo.get_or_create_artist(
            self.db,
            name,
            spotify_id=spotify_id,
            image_url=_largest_image(payload.get("images")),
        )
        if existing is None:
            self.stats.artists_created += 1
        if genres:
            repo.link_genres(self.db, "artist_genres", "artist_id", artist_id, genres, source="spotify")
        if spotify_id:
            self._known_artists[spotify_id] = artist_id
        return artist_id

    def _credits(self, payloads: Iterable[dict[str, Any]]) -> list[int]:
        return [self._artist(payload) for payload in payloads or [] if payload.get("name")]

    def _album(self, payload: dict[str, Any]) -> int | None:
        if not payload or not payload.get("name"):
            return None
        spotify_id = payload.get("id")
        if spotify_id and spotify_id in self._known_albums:
            return self._known_albums[spotify_id]
        artist_ids = self._credits(payload.get("artists") or [])
        album_artist = artist_ids[0] if artist_ids else None
        existing = None
        if spotify_id:
            existing = self.db.one("SELECT id FROM albums WHERE spotify_id = ?", (spotify_id,))
        album_id = repo.get_or_create_album(
            self.db,
            payload["name"],
            artist_id=album_artist,
            spotify_id=spotify_id,
            album_type=payload.get("album_type"),
            release_date=payload.get("release_date"),
            release_year=_release_year(payload.get("release_date")),
            total_tracks=payload.get("total_tracks"),
            cover_url=_largest_image(payload.get("images")),
        )
        if existing is None:
            self.stats.albums_created += 1
        repo.link_credits(self.db, "album_artists", "album_id", album_id, artist_ids)
        if spotify_id:
            self._known_albums[spotify_id] = album_id
        return album_id

    def _track(self, payload: dict[str, Any]) -> int | None:
        if not payload or not payload.get("name"):
            return None
        if payload.get("is_local"):
            return None
        spotify_id = payload.get("id")
        if spotify_id and spotify_id in self._known_tracks:
            return self._known_tracks[spotify_id]
        artist_ids = self._credits(payload.get("artists") or [])
        album_id = self._album(payload.get("album") or {})
        existing = None
        if spotify_id:
            existing = self.db.one("SELECT id FROM tracks WHERE spotify_id = ?", (spotify_id,))
        track_id = repo.get_or_create_track(
            self.db,
            payload["name"],
            artist_id=artist_ids[0] if artist_ids else None,
            album_id=album_id,
            spotify_id=spotify_id,
            isrc=(payload.get("external_ids") or {}).get("isrc"),
            disc_number=payload.get("disc_number"),
            track_number=payload.get("track_number"),
            duration_ms=payload.get("duration_ms"),
            explicit=payload.get("explicit"),
            source_url=(payload.get("external_urls") or {}).get("spotify"),
        )
        if existing is None:
            self.stats.tracks_created += 1
        repo.link_credits(self.db, "track_artists", "track_id", track_id, artist_ids)
        if spotify_id:
            self._known_tracks[spotify_id] = track_id
        return track_id

    # -------------------------------------------------------------- imports
    def import_saved_tracks(self) -> None:
        for item in self.client.saved_tracks():
            track_id = self._track(item.get("track") or {})
            if track_id is None:
                continue
            self.db.execute(
                "INSERT INTO spotify_saved_tracks(track_id, added_at, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(track_id) DO UPDATE SET added_at = excluded.added_at, updated_at = excluded.updated_at",
                (track_id, item.get("added_at"), utcnow()),
            )
            self.stats.saved_tracks += 1

    def import_saved_albums(self) -> None:
        for item in self.client.saved_albums():
            album = item.get("album") or {}
            album_id = self._album(album)
            if album_id is None:
                continue
            self.db.execute(
                "INSERT INTO spotify_saved_albums(album_id, added_at, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(album_id) DO UPDATE SET added_at = excluded.added_at, updated_at = excluded.updated_at",
                (album_id, item.get("added_at"), utcnow()),
            )
            self.stats.saved_albums += 1

    def import_playlists(self) -> None:
        me_id = self.client.me().get("id")
        for playlist in self.client.playlists():
            spotify_id = playlist.get("id")
            if not spotify_id:
                continue
            owner_id = (playlist.get("owner") or {}).get("id")
            collaborative = bool(playlist.get("collaborative"))
            if owner_id == me_id and collaborative:
                kind = "collaborative"
            elif owner_id == me_id:
                kind = "personal"
            else:
                kind = "followed"
            existing = self.db.one(
                "SELECT id, snapshot_id FROM playlists WHERE spotify_id = ?", (spotify_id,)
            )
            previous_snapshot = existing["snapshot_id"] if existing else None
            playlist_id = repo.get_or_create_playlist(
                self.db,
                playlist.get("name") or "Untitled playlist",
                spotify_id=spotify_id,
                owner_id=owner_id,
                owner_name=(playlist.get("owner") or {}).get("display_name"),
                description=playlist.get("description"),
                public=playlist.get("public"),
                collaborative=collaborative,
                snapshot_id=playlist.get("snapshot_id"),
                kind=kind,
                total_tracks=int((playlist.get("items") or playlist.get("tracks") or {}).get("total") or 0),
            )
            self.stats.playlists += 1

            can_read_items = kind in ("personal", "collaborative")
            if not can_read_items:
                self.stats.skipped_playlists.append(
                    f"{playlist.get('name')} (followed playlists do not expose items in dev mode)"
                )
                self.db.execute(
                    "UPDATE playlists SET last_synced_at = ?, updated_at = ? WHERE id = ?",
                    (utcnow(), utcnow(), playlist_id),
                )
                continue
            if previous_snapshot and previous_snapshot == playlist.get("snapshot_id"):
                continue

            items = self.client.playlist_items(spotify_id)
            rows: list[tuple[int, int, str | None, str | None]] = []
            for position, raw in enumerate(items):
                track = normalize_playlist_item(raw)
                if track is None:
                    continue
                track_id = self._track(track)
                if track_id is None:
                    continue
                rows.append((track_id, position, track.get("added_at"), track.get("added_by")))
            if rows:
                repo.replace_playlist_tracks(self.db, playlist_id, rows)
                self.stats.playlist_items += len(rows)
            self.db.execute(
                "UPDATE playlists SET snapshot_id = ?, total_tracks = ?, last_synced_at = ?, updated_at = ? WHERE id = ?",
                (playlist.get("snapshot_id"), len(rows), utcnow(), utcnow(), playlist_id),
            )

    def import_followed_artists(self) -> None:
        try:
            artists = list(self.client.followed_artists())
        except SpotifyUnavailable as exc:
            self.stats.warnings.append(f"Followed artists unavailable: {exc}")
            return
        for artist in artists:
            artist_id = self._artist(artist)
            self.db.execute(
                "INSERT OR REPLACE INTO spotify_followed_artists(artist_id, captured_at) VALUES (?, ?)",
                (artist_id, utcnow()),
            )
            self.stats.followed_artists += 1

    def import_top_items(self) -> None:
        for time_range in ("short_term", "medium_term", "long_term"):
            for rank, artist in enumerate(self.client.top_artists(time_range, limit=50), start=1):
                artist_id = self._artist(artist)
                self.db.execute(
                    "INSERT INTO spotify_top_items(entity_type, entity_id, time_range, rank) VALUES ('artist', ?, ?, ?)",
                    (artist_id, time_range, rank),
                )
                self.stats.top_artists += 1
            for rank, track in enumerate(self.client.top_tracks(time_range, limit=50), start=1):
                track_id = self._track(track)
                if track_id is None:
                    continue
                self.db.execute(
                    "INSERT INTO spotify_top_items(entity_type, entity_id, time_range, rank) VALUES ('track', ?, ?, ?)",
                    (track_id, time_range, rank),
                )
                self.stats.top_tracks += 1

    def import_recently_played(self) -> None:
        for item in self.client.recently_played(limit=50):
            track = item.get("track") or {}
            track_id = self._track(track)
            if track_id is None:
                continue
            self.db.execute(
                "INSERT OR IGNORE INTO spotify_recently_played(track_id, played_at, context_uri) VALUES (?, ?, ?)",
                (track_id, item.get("played_at"), (item.get("context") or {}).get("uri")),
            )
            self.stats.recently_played += 1

    def import_seed_playlists(self) -> None:
        me_id = self.client.me().get("id")
        for url in self.config.spotify.seed_playlists:
            playlist_id = _playlist_id_from_url(url)
            if not playlist_id:
                continue
            try:
                playlist = self.client.get(f"/playlists/{playlist_id}")
            except Exception as exc:
                self.stats.warnings.append(f"Seed playlist {url} unavailable: {exc}")
                continue
            owner_id = (playlist.get("owner") or {}).get("id")
            kind = "personal" if owner_id == me_id else "seed"
            db_id = repo.get_or_create_playlist(
                self.db,
                playlist.get("name") or f"Seed {playlist_id}",
                spotify_id=playlist_id,
                owner_id=owner_id,
                owner_name=(playlist.get("owner") or {}).get("display_name"),
                description=playlist.get("description"),
                public=playlist.get("public"),
                kind=kind,
                total_tracks=int((playlist.get("items") or {}).get("total") or 0),
            )
            items = self.client.playlist_items(playlist_id, max_items=500)
            rows = []
            for position, raw in enumerate(items):
                track = normalize_playlist_item(raw)
                if track is None:
                    continue
                track_id = self._track(track)
                if track_id is None:
                    continue
                rows.append((track_id, position, track.get("added_at"), track.get("added_by")))
            if rows:
                repo.replace_playlist_tracks(self.db, db_id, rows)
                self.stats.playlist_items += len(rows)
            self.db.execute(
                "UPDATE playlists SET last_synced_at = ?, total_tracks = ?, updated_at = ? WHERE id = ?",
                (utcnow(), len(rows), utcnow(), db_id),
            )

    # ------------------------------------------------------------------ run
    def run(self, include_seeds: bool = True, full: bool = False) -> ImportStats:
        cursor = self.db.execute(
            "INSERT INTO spotify_imports(kind, status) VALUES (?, 'running')",
            ("full" if full else "sync",),
        )
        import_id = int(cursor.lastrowid)
        try:
            self.client.me(refresh=True)
            self.import_saved_tracks()
            self.import_saved_albums()
            self.import_playlists()
            self.import_followed_artists()
            self.import_top_items()
            self.import_recently_played()
            if include_seeds:
                self.import_seed_playlists()
        except Exception as exc:
            self.db.execute(
                "UPDATE spotify_imports SET status = 'failed', finished_at = ?, error = ? WHERE id = ?",
                (utcnow(), str(exc), import_id),
            )
            raise
        summary = self.stats.as_dict()
        self.db.execute(
            "UPDATE spotify_imports SET status = 'completed', finished_at = ?, items = ?, summary = ?, error = NULL WHERE id = ?",
            (
                utcnow(),
                self.stats.saved_tracks
                + self.stats.saved_albums
                + self.stats.playlist_items
                + self.stats.followed_artists
                + self.stats.recently_played,
                json.dumps(summary),
                import_id,
            ),
        )
        self.db.set_setting(LAST_SYNC_KEY, utcnow())
        self.db.set_setting(IMPORT_SUMMARY_KEY, json.dumps(summary))
        self.db.execute("INSERT INTO spotify_snapshots(payload) VALUES (?)", (json.dumps(summary),))
        return self.stats


def _playlist_id_from_url(url: str) -> str | None:
    value = url.strip()
    if value.startswith("spotify:playlist:"):
        return value.split(":")[-1]
    if "open.spotify.com" in value:
        from urllib.parse import urlparse

        parts = [p for p in urlparse(value).path.split("/") if p]
        if "playlist" in parts:
            index = parts.index("playlist")
            if index + 1 < len(parts):
                return parts[index + 1].split("?")[0]
    if len(value) == 22 and " " not in value:
        return value
    return None


def detect_changes(config: Config, db: Database, client: SpotifyClient) -> dict[str, Any]:
    """Compare Spotify state with the local database without importing."""
    last_sync = db.get_setting(LAST_SYNC_KEY)
    saved = list(client.saved_tracks())
    known_ids = {
        row["spotify_id"]
        for row in db.query(
            "SELECT t.spotify_id FROM spotify_saved_tracks s JOIN tracks t ON t.id = s.track_id"
        )
        if row["spotify_id"]
    }
    new_saved = [
        {
            "name": (item.get("track") or {}).get("name"),
            "artist": primary_artist(item.get("track") or {}).get("name"),
            "added_at": item.get("added_at"),
        }
        for item in saved
        if (item.get("track") or {}).get("id") not in known_ids
    ]
    playlist_changes = []
    for playlist in client.playlists():
        row = db.one("SELECT snapshot_id FROM playlists WHERE spotify_id = ?", (playlist.get("id"),))
        if row is None:
            playlist_changes.append({"playlist": playlist.get("name"), "change": "new"})
        elif row["snapshot_id"] != playlist.get("snapshot_id"):
            playlist_changes.append({"playlist": playlist.get("name"), "change": "updated"})
    return {
        "last_sync": last_sync,
        "new_saved_tracks": new_saved,
        "playlist_changes": playlist_changes,
    }


def status_summary(db: Database) -> dict[str, Any]:
    summary = db.get_setting(IMPORT_SUMMARY_KEY)
    return {
        "client_id_configured": db.get_setting(CLIENT_ID_SETTING) is not None,
        "last_sync": db.get_setting(LAST_SYNC_KEY),
        "last_import": json.loads(summary) if summary else None,
        "counts": {
            "artists": db.scalar("SELECT COUNT(*) FROM artists"),
            "albums": db.scalar("SELECT COUNT(*) FROM albums"),
            "tracks": db.scalar("SELECT COUNT(*) FROM tracks"),
            "playlists": db.scalar("SELECT COUNT(*) FROM playlists"),
            "saved_tracks": db.scalar("SELECT COUNT(*) FROM spotify_saved_tracks"),
            "recently_played": db.scalar("SELECT COUNT(*) FROM spotify_recently_played"),
        },
    }

