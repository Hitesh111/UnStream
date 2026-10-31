"""Discovery engine: build explained candidates from Spotify + public metadata."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from fiiolib.config import Config
from fiiolib.db import Database, repo, utcnow
from fiiolib.discovery.internet_archive import ArchiveItem, InternetArchiveClient
from fiiolib.discovery.musicbrainz import MusicBrainzClient
from fiiolib.spotify.client import SpotifyClient, SpotifyUnavailable
from fiiolib.spotify.taste import PROFILE_KEY, TasteProfiler

logger = logging.getLogger(__name__)

SOURCE_IDS = {"internet_archive": 1, "jamendo": 2, "free_music_archive": 3, "bandcamp": 4}


@dataclass
class DiscoverySummary:
    artists: int = 0
    albums: int = 0
    releases: int = 0
    labels: int = 0
    warnings: list[str] = field(default_factory=list)
    spotify_artists_considered: int = 0
    external_artists_considered: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "artists": self.artists,
            "albums": self.albums,
            "releases": self.releases,
            "labels": self.labels,
            "warnings": self.warnings,
            "spotify_artists_considered": self.spotify_artists_considered,
            "external_artists_considered": self.external_artists_considered,
        }


def _reason(code: str, text: str) -> dict[str, str]:
    return {"code": code, "text": text}


def _era_of(year: int | None) -> str | None:
    if not year:
        return None
    if year < 1970:
        return "pre-1970"
    if year < 1980:
        return "1970s"
    if year < 1990:
        return "1980s"
    if year < 2000:
        return "1990s"
    if year < 2010:
        return "2000s"
    if year < 2020:
        return "2010s"
    return "2020s"


class DiscoveryEngine:
    def __init__(
        self,
        config: Config,
        db: Database,
        spotify: SpotifyClient | None = None,
        musicbrainz: MusicBrainzClient | None = None,
        internet_archive: InternetArchiveClient | None = None,
    ) -> None:
        self.config = config
        self.db = db
        self.spotify = spotify
        self.mb = musicbrainz or MusicBrainzClient(
            config.discovery.musicbrainz_user_agent,
            min_interval=config.discovery.musicbrainz_min_interval_seconds,
            cache_dir=config.root / "cache" / "musicbrainz",
        )
        self.ia = internet_archive or InternetArchiveClient(
            config.discovery.musicbrainz_user_agent,
            min_interval=config.discovery.internet_archive_min_interval_seconds,
            cache_dir=config.root / "cache" / "internet_archive",
        )

    def close(self) -> None:
        self.mb.close()
        self.ia.close()

    # ------------------------------------------------------------ helpers
    def _profile(self) -> dict[str, Any]:
        profile = self.db.get_preference(PROFILE_KEY)
        if not isinstance(profile, dict) or profile.get("empty"):
            profile = TasteProfiler(self.config, self.db).build()
        return profile or {}

    def _artist_rows(self, limit: int) -> list[dict[str, Any]]:
        profile = self._profile()
        rows: list[dict[str, Any]] = []
        for item in (profile.get("top_artists") or [])[:limit]:
            row = self.db.one(
                "SELECT id, name, spotify_id FROM artists WHERE id = ?", (item["artist_id"],)
            )
            if row:
                rows.append({"id": row["id"], "name": row["name"], "spotify_id": row["spotify_id"],
                             "weight": float(item["weight"]), "signals": item.get("signals", {})})
        return rows

    def _playlist_overlap(self) -> dict[int, int]:
        return {
            int(row["artist_id"]): int(row["n"])
            for row in self.db.query(
                "SELECT ta.artist_id, COUNT(DISTINCT p.id) AS n FROM playlist_tracks pt "
                "JOIN playlists p ON p.id = pt.playlist_id "
                "JOIN track_artists ta ON ta.track_id = pt.track_id GROUP BY ta.artist_id"
            )
        }

    def _library_years(self) -> list[int]:
        return [
            int(row["release_year"])
            for row in self.db.query(
                "SELECT DISTINCT release_year FROM albums WHERE release_year IS NOT NULL"
            )
        ]

    def _album_has_audio(self, album_id: int) -> bool:
        return bool(
            self.db.scalar(
                "SELECT 1 FROM files f JOIN tracks t ON t.id = f.track_id "
                "WHERE t.album_id = ? AND f.validated = 1 LIMIT 1",
                (album_id,),
            )
        )

    # ------------------------------------------------------------ Spotify
    def discover_from_spotify(self, limit_artists: int = 15) -> DiscoverySummary:
        summary = DiscoverySummary()
        if self.spotify is None:
            summary.warnings.append("Spotify client unavailable; skipped Spotify discovery")
            return summary
        artists = self._artist_rows(limit_artists)
        overlap = self._playlist_overlap()
        years = self._library_years()
        earliest = min(years) if years else None
        latest = max(years) if years else None
        max_weight = max((item["weight"] for item in artists), default=1.0) or 1.0
        era_profile = {item["era"]: item["weight"] for item in self._profile().get("era_profile", [])}
        dominant_era = max(era_profile, key=era_profile.get) if era_profile else None
        current_year = datetime.now(UTC).year

        related_seen: set[str] = set()
        for artist in artists:
            summary.spotify_artists_considered += 1
            base = 10.0 + 40.0 * (artist["weight"] / max_weight)
            reasons = [
                _reason("repeated_artist", f"Weighted taste score {artist['weight']:.1f} for {artist['name']}"),
            ]
            plays = overlap.get(artist["id"], 0)
            if plays > 1:
                reasons.append(_reason("playlist_overlap", f"Appears in {plays} playlists you maintain"))
            if artist["signals"].get("saved_tracks"):
                reasons.append(_reason("saved_artist", "You have saved tracks by this artist"))

            if not artist["spotify_id"]:
                continue
            try:
                albums = self.spotify.artist_albums(artist["spotify_id"], limit=50)
            except Exception as exc:
                summary.warnings.append(f"Artist albums unavailable for {artist['name']}: {exc}")
                continue

            for album in albums:
                if not album.get("id") or album.get("album_type") == "appears_on":
                    continue
                album_reasons = list(reasons)
                album_year = _safe_year(album.get("release_date"))
                if album_year and earliest and album_year < earliest:
                    album_reasons.append(_reason("earlier_release", f"Earlier release ({album_year}) than your current collection"))
                elif album_year and latest and album_year > latest:
                    album_reasons.append(_reason("later_release", f"Later release ({album_year}) than your current collection"))
                if album_year and _era_of(album_year) == dominant_era:
                    album_reasons.append(_reason("era_match", f"Matches your {dominant_era} listening preference"))
                if album_year and album_year >= current_year - 1:
                    album_reasons.append(_reason("recent_release", f"Recent release from {artist['name']}"))
                album_reasons.append(_reason("not_owned", "Not present in your verified library"))

                db_album_id = repo.get_or_create_album(
                    self.db,
                    album.get("name") or "Untitled",
                    artist_id=artist["id"],
                    spotify_id=album.get("id"),
                    album_type=album.get("album_type"),
                    release_date=album.get("release_date"),
                    release_year=album_year,
                    total_tracks=album.get("total_tracks"),
                    cover_url=(album.get("images") or [{}])[0].get("url") if album.get("images") else None,
                )
                if self._album_has_audio(db_album_id):
                    continue
                score = base + (2.0 if plays > 1 else 0.0)
                if album_year and _era_of(album_year) == dominant_era:
                    score += 3.0
                if album_year and album_year >= current_year - 1:
                    score += 2.0
                repo.upsert_candidate(
                    self.db,
                    "album",
                    album.get("name") or "Untitled",
                    album.get("id"),
                    round(score, 2),
                    album_reasons,
                    artist_id=artist["id"],
                    album_id=db_album_id,
                )
                summary.albums += 1

            try:
                related = self.spotify.related_artists(artist["spotify_id"])
            except SpotifyUnavailable:
                related = [
                    {"_unavailable": True}
                ]  # sentinel handled below
            except Exception as exc:
                summary.warnings.append(f"Related artists unavailable: {exc}")
                related = []
            if related and related[0].get("_unavailable"):
                summary.warnings.append(
                    "Spotify related-artists endpoint unavailable to this app; "
                    "using MusicBrainz/Internet Archive links instead"
                )
                related = []
            for rel in related:
                rel_id = rel.get("id")
                if not rel_id or rel_id in related_seen:
                    continue
                related_seen.add(rel_id)
                rel_db_id = repo.get_or_create_artist(
                    self.db,
                    rel.get("name") or "Unknown Artist",
                    spotify_id=rel_id,
                    image_url=(rel.get("images") or [{}])[0].get("url") if rel.get("images") else None,
                )
                repo.link_genres(
                    self.db, "artist_genres", "artist_id", rel_db_id, rel.get("genres") or [], source="spotify"
                )
                score = base * 0.55
                repo.upsert_candidate(
                    self.db,
                    "artist",
                    rel.get("name") or "Unknown Artist",
                    rel_id,
                    round(score, 2),
                    [_reason("related_artist", f"Related to {artist['name']}, a heavily weighted artist for you")],
                    artist_id=rel_db_id,
                )
                summary.artists += 1
        return summary

    # ------------------------------------------------------- MusicBrainz
    def discover_from_musicbrainz(self, limit_artists: int = 8) -> DiscoverySummary:
        summary = DiscoverySummary()
        artists = self._artist_rows(limit_artists)
        max_weight = max((item["weight"] for item in artists), default=1.0) or 1.0
        for artist in artists:
            try:
                matches = self.mb.search_artists(artist["name"], limit=3)
            except Exception as exc:
                summary.warnings.append(f"MusicBrainz artist lookup failed for {artist['name']}: {exc}")
                continue
            if not matches:
                continue
            match = matches[0]
            mbid = match.get("id")
            if not mbid:
                continue
            self.db.execute(
                "UPDATE artists SET musicbrainz_id = COALESCE(musicbrainz_id, ?) WHERE id = ?",
                (mbid, artist["id"]),
            )
            try:
                groups = self.mb.release_groups(mbid, limit=60)
            except Exception as exc:
                summary.warnings.append(f"MusicBrainz release groups failed for {artist['name']}: {exc}")
                continue
            summary.external_artists_considered += 1
            base = 8.0 + 30.0 * (artist["weight"] / max_weight)
            for group in groups[: self.config.discovery.max_candidates_per_artist * 4]:
                if not group.mbid:
                    continue
                existing = self.db.one(
                    "SELECT 1 FROM albums WHERE musicbrainz_release_group = ?", (group.mbid,)
                )
                if existing:
                    continue
                reasons = [
                    _reason("musicbrainz_release", f"MusicBrainz release group by {artist['name']}"),
                    _reason("not_owned", "Not present in your verified library"),
                ]
                if group.primary_type == "album":
                    reasons.append(_reason("album_priority", "Complete album release"))
                if group.year:
                    reasons.append(_reason("release_year", f"Released {group.year}"))
                score = base
                if group.primary_type == "album":
                    score += 2.0
                repo.upsert_candidate(
                    self.db,
                    "album",
                    group.title,
                    f"mb:{group.mbid}",
                    round(score, 2),
                    reasons,
                    artist_id=artist["id"],
                )
                summary.albums += 1
        return summary

    # ------------------------------------------------- Internet Archive
    def discover_from_internet_archive(self, limit_artists: int = 6, download_limit: int = 10) -> DiscoverySummary:
        summary = DiscoverySummary()
        artists = self._artist_rows(limit_artists)
        max_weight = max((item["weight"] for item in artists), default=1.0) or 1.0
        source_id = self._source_id("internet_archive")
        for artist in artists:
            try:
                docs = self.ia.search_audio_by_creator(artist["name"], rows=25)
            except Exception as exc:
                summary.warnings.append(f"Internet Archive search failed for {artist['name']}: {exc}")
                continue
            items = self.ia.items_from_docs(docs)
            base = 6.0 + 24.0 * (artist["weight"] / max_weight)
            for item in items:
                candidate_id = self._upsert_archive_candidate(item, artist, base, summary)
                if candidate_id is None:
                    continue
                self._upsert_archive_release(item, candidate_id, artist, source_id, summary)
        return summary

    def _upsert_archive_candidate(
        self,
        item: ArchiveItem,
        artist: dict[str, Any],
        base: float,
        summary: DiscoverySummary,
    ) -> int | None:
        verdict = item.verdict
        if verdict is None:
            return None
        if verdict.needs_manual_review and not self.config.discovery.allow_manual_review_candidates:
            return None
        reasons = [
            _reason("archive_creator", f"Internet Archive audio credited to {artist['name']}"),
            _reason("licence", verdict.reason),
        ]
        if verdict.allows_redistribution:
            reasons.append(_reason("authorised", f"Authorised distribution ({verdict.licence})"))
        else:
            reasons.append(_reason("manual_review", "Rights unclear; manual verification required"))
        score = base
        if verdict.allows_redistribution:
            score += 5.0
        candidate_id = repo.upsert_candidate(
            self.db,
            "album",
            item.title,
            f"ia:{item.identifier}",
            round(score, 2),
            reasons,
            artist_id=artist["id"],
        )
        summary.albums += 1
        return candidate_id

    def _upsert_archive_release(
        self,
        item: ArchiveItem,
        candidate_id: int,
        artist: dict[str, Any],
        source_id: int,
        summary: DiscoverySummary,
    ) -> None:
        verdict = item.verdict
        if verdict is None:
            return
        media_type, quality = item.best_format()
        repo.upsert_release(
            self.db,
            source_id=source_id,
            external_id=item.identifier,
            title=item.title,
            artist_name=item.creator,
            candidate_id=candidate_id,
            media_type=media_type,
            quality=quality,
            size_bytes=item.total_audio_bytes() or None,
            download_url=item.torrent_url,
            licence=verdict.licence,
            licence_url=verdict.licence_url,
            redistribution_allowed=verdict.allows_redistribution,
            authorisation=verdict.authorisation,
            needs_manual_review=verdict.needs_manual_review,
            metadata={
                "page_url": item.page_url,
                "collections": item.collections,
                "rights": item.rights,
                "downloads": item.downloads,
                "reason": verdict.reason,
            },
        )
        summary.releases += 1

    def _source_id(self, name: str) -> int:
        value = self.db.scalar("SELECT id FROM sources WHERE name = ?", (name,))
        if value is None:
            cursor = self.db.execute(
                "INSERT INTO sources(name, kind, base_url) VALUES (?, 'archive', 'https://archive.org')",
                (name,),
            )
            return int(cursor.lastrowid)
        return int(value)

    # ------------------------------------------------------------- labels
    def label_report(self) -> list[dict[str, Any]]:
        rows = self.db.query(
            "SELECT l.name, COUNT(DISTINCT al.id) AS albums, COUNT(DISTINCT aa.artist_id) AS artists "
            "FROM labels l JOIN albums al ON al.label_id = l.id "
            "LEFT JOIN album_artists aa ON aa.album_id = al.id "
            "GROUP BY l.name ORDER BY albums DESC LIMIT 100"
        )
        return [dict(row) for row in rows]

    # ---------------------------------------------------------------- run
    def run(
        self,
        limit_artists: int = 12,
        use_spotify: bool = True,
        use_musicbrainz: bool = True,
        use_internet_archive: bool = True,
    ) -> DiscoverySummary:
        totals = DiscoverySummary()
        if use_spotify:
            _merge(totals, self.discover_from_spotify(limit_artists))
        if use_musicbrainz:
            _merge(totals, self.discover_from_musicbrainz(min(limit_artists, 8)))
        if use_internet_archive:
            _merge(totals, self.discover_from_internet_archive(min(limit_artists, 6)))
        totals.labels = len(self.label_report())
        self.db.record_log("INFO", "discovery", "Discovery run complete", totals.as_dict())
        self.db.set_setting("discovery.last_run_at", utcnow())
        return totals


def _merge(target: DiscoverySummary, source: DiscoverySummary) -> None:
    target.artists += source.artists
    target.albums += source.albums
    target.releases += source.releases
    target.labels += source.labels
    target.warnings.extend(source.warnings)
    target.spotify_artists_considered += source.spotify_artists_considered
    target.external_artists_considered += source.external_artists_considered


def _safe_year(release_date: str | None) -> int | None:
    if not release_date:
        return None
    try:
        return int(str(release_date)[:4])
    except ValueError:
        return None


def candidate_rows(
    db: Database,
    status: str | None = None,
    limit: int = 100,
    query: str | None = None,
) -> list[dict[str, Any]]:
    sql = (
        "SELECT c.*, ar.name AS artist_name, al.cover_url, al.release_year "
        "FROM discovery_candidates c "
        "LEFT JOIN artists ar ON ar.id = c.artist_id "
        "LEFT JOIN albums al ON al.id = c.album_id"
    )
    clauses: list[str] = []
    params: list[Any] = []
    if status and status.lower() not in ("all", "any", ""):
        clauses.append("c.status = ?")
        params.append(status)
    if query and query.strip():
        q_wild = f"%{query.strip()}%"
        clauses.append("(c.entity_name LIKE ? OR ar.name LIKE ? OR c.reasons LIKE ?)")
        params.extend([q_wild, q_wild, q_wild])
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY c.score DESC LIMIT ?"
    params.append(limit)
    rows = []
    for row in db.query(sql, params):
        item = dict(row)
        try:
            item["reasons"] = json.loads(item.get("reasons") or "[]")
        except json.JSONDecodeError:
            item["reasons"] = []
        rows.append(item)
    return rows
