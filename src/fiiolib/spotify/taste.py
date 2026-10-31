"""Weighted music-taste profile derived from real Spotify imports."""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from typing import Any

from fiiolib.config import Config
from fiiolib.db import Database, utcnow

logger = logging.getLogger(__name__)

PROFILE_KEY = "taste.profile"

SOURCE_WEIGHTS = {
    "saved_track": 3.0,
    "saved_album": 2.0,
    "personal_playlist": 1.5,
    "collaborative_playlist": 1.2,
    "followed_playlist": 0.5,
    "seed_playlist": 0.3,
    "followed_artist": 2.0,
    "recent_play": 0.6,
    "top_item": 1.0,
}

TOP_RANGE_WEIGHT = {"short_term": 1.0, "medium_term": 0.8, "long_term": 0.5}

INSTRUMENTAL_HINTS = (
    "instrumental",
    "ambient",
    "downtempo",
    "psybient",
    "minimal",
    "soundtrack",
    "classical",
    "sitar",
    "raga",
    "drone",
    "post-rock",
    "space rock",
    "krautrock",
)


def _decay(days: float, half_life_days: float = 180.0) -> float:
    if days <= 0:
        return 1.0
    return 0.5 ** (days / half_life_days)


def _latest_top_capture(db: Database) -> str | None:
    return db.scalar("SELECT MAX(captured_at) FROM spotify_top_items")


class TasteProfiler:
    def __init__(self, config: Config, db: Database) -> None:
        self.config = config
        self.db = db

    # ------------------------------------------------------------- gathering
    def artist_weights(self) -> dict[int, float]:
        weights: dict[int, float] = defaultdict(float)
        contributions: dict[int, dict[str, float]] = defaultdict(lambda: defaultdict(float))

        for row in self.db.query(
            "SELECT ta.artist_id, COUNT(*) AS n FROM spotify_saved_tracks s "
            "JOIN track_artists ta ON ta.track_id = s.track_id GROUP BY ta.artist_id"
        ):
            value = row["n"] * SOURCE_WEIGHTS["saved_track"]
            weights[row["artist_id"]] += value
            contributions[row["artist_id"]]["saved_tracks"] += value

        for row in self.db.query(
            "SELECT aa.artist_id, COUNT(*) AS n FROM spotify_saved_albums s "
            "JOIN album_artists aa ON aa.album_id = s.album_id GROUP BY aa.artist_id"
        ):
            value = row["n"] * SOURCE_WEIGHTS["saved_album"]
            weights[row["artist_id"]] += value
            contributions[row["artist_id"]]["saved_albums"] += value

        playlist_weight = {
            "personal": SOURCE_WEIGHTS["personal_playlist"],
            "collaborative": SOURCE_WEIGHTS["collaborative_playlist"],
            "followed": SOURCE_WEIGHTS["followed_playlist"],
            "seed": SOURCE_WEIGHTS["seed_playlist"],
        }
        for row in self.db.query(
            "SELECT ta.artist_id, p.kind, COUNT(DISTINCT p.id) AS playlists, COUNT(*) AS n "
            "FROM playlist_tracks pt JOIN playlists p ON p.id = pt.playlist_id "
            "JOIN track_artists ta ON ta.track_id = pt.track_id GROUP BY ta.artist_id, p.kind"
        ):
            factor = playlist_weight.get(row["kind"], 0.2)
            value = row["n"] * factor
            weights[row["artist_id"]] += value
            contributions[row["artist_id"]][f"{row['kind']}_playlists"] += value

        for row in self.db.query(
            "SELECT a.id, a.spotify_id FROM spotify_followed_artists f JOIN artists a ON a.id = f.artist_id"
        ):
            weights[row["id"]] += SOURCE_WEIGHTS["followed_artist"]
            contributions[row["id"]]["followed"] += SOURCE_WEIGHTS["followed_artist"]

        capture = _latest_top_capture(self.db)
        if capture:
            for row in self.db.query(
                "SELECT ti.entity_id, ti.time_range, ti.rank FROM spotify_top_items ti "
                "WHERE ti.entity_type = 'artist' AND ti.captured_at = ?",
                (capture,),
            ):
                rank_score = max(0.0, 1.0 - (row["rank"] - 1) / 50.0)
                value = rank_score * TOP_RANGE_WEIGHT.get(row["time_range"], 0.7) * SOURCE_WEIGHTS["top_item"]
                weights[row["entity_id"]] += value
                contributions[row["entity_id"]]["top_items"] += value

        plays: dict[int, int] = defaultdict(int)
        for row in self.db.query(
            "SELECT ta.artist_id, COUNT(*) AS n FROM spotify_recently_played rp "
            "JOIN track_artists ta ON ta.track_id = rp.track_id GROUP BY ta.artist_id"
        ):
            plays[row["artist_id"]] = int(row["n"])
        for artist_id, count in plays.items():
            value = min(count, 5) * SOURCE_WEIGHTS["recent_play"]
            weights[artist_id] += value
            contributions[artist_id]["recent_plays"] += value

        self._contributions = contributions
        return dict(weights)

    # -------------------------------------------------------------- analysis
    def build(self, limit: int = 25) -> dict[str, Any]:
        self._contributions = defaultdict(lambda: defaultdict(float))
        weights = self.artist_weights()
        if not weights:
            profile = {
                "generated_at": utcnow(),
                "empty": True,
                "message": "No Spotify data imported yet. Run: musicctl spotify sync",
            }
            self.db.set_preference(PROFILE_KEY, profile)
            return profile

        artists = {
            int(row["id"]): row
            for row in self.db.query("SELECT id, name, spotify_id FROM artists")
        }
        ranked_artists = sorted(
            (
                {
                    "artist_id": artist_id,
                    "name": artists[artist_id]["name"] if artist_id in artists else str(artist_id),
                    "spotify_id": artists[artist_id]["spotify_id"] if artist_id in artists else None,
                    "weight": round(weight, 3),
                    "signals": dict(self._contributions.get(artist_id, {})),
                }
                for artist_id, weight in weights.items()
            ),
            key=lambda item: item["weight"],
            reverse=True,
        )

        genre_weights: dict[str, float] = defaultdict(float)
        for row in self.db.query(
            "SELECT ag.artist_id, g.name, ag.weight FROM artist_genres ag JOIN genres g ON g.id = ag.genre_id"
        ):
            if row["artist_id"] in weights:
                genre_weights[row["name"]] += weights[row["artist_id"]] * float(row["weight"] or 1.0)

        label_weights: dict[str, float] = defaultdict(float)
        for row in self.db.query(
            "SELECT a.artist_id, l.name, COUNT(*) AS n FROM albums al "
            "JOIN labels l ON l.id = al.label_id JOIN album_artists a ON a.album_id = al.id "
            "GROUP BY a.artist_id, l.name"
        ):
            if row["artist_id"] in weights:
                label_weights[row["name"]] += weights[row["artist_id"]] * row["n"]

        era_weights: dict[str, float] = defaultdict(float)
        for row in self.db.query(
            "SELECT aa.artist_id, al.release_year FROM albums al "
            "JOIN album_artists aa ON aa.album_id = al.id WHERE al.release_year IS NOT NULL"
        ):
            if row["artist_id"] not in weights:
                continue
            year = int(row["release_year"])
            if year < 1970:
                bucket = "pre-1970"
            elif year < 1980:
                bucket = "1970s"
            elif year < 1990:
                bucket = "1980s"
            elif year < 2000:
                bucket = "1990s"
            elif year < 2010:
                bucket = "2000s"
            elif year < 2020:
                bucket = "2010s"
            else:
                bucket = "2020s"
            era_weights[bucket] += weights[row["artist_id"]]

        playlist_overlap = {
            row["artist_id"]: row["playlists"]
            for row in self.db.query(
                "SELECT ta.artist_id, COUNT(DISTINCT p.id) AS playlists FROM playlist_tracks pt "
                "JOIN playlists p ON p.id = pt.playlist_id JOIN track_artists ta ON ta.track_id = pt.track_id "
                "GROUP BY ta.artist_id"
            )
        }

        recent_cutoff = (datetime.now(UTC) - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
        recent_artists = {
            row["artist_id"]
            for row in self.db.query(
                "SELECT DISTINCT ta.artist_id FROM spotify_recently_played rp "
                "JOIN track_artists ta ON ta.track_id = rp.track_id WHERE rp.played_at >= ?",
                (recent_cutoff,),
            )
        }

        instrumental_score = 0.0
        vocal_score = 0.0
        for genre, weight in genre_weights.items():
            if any(hint in genre.lower() for hint in INSTRUMENTAL_HINTS):
                instrumental_score += weight
            else:
                vocal_score += weight
        total = instrumental_score + vocal_score
        instrumental_ratio = round(instrumental_score / total, 3) if total else None

        profile = {
            "generated_at": utcnow(),
            "empty": False,
            "totals": {
                "artists": len(weights),
                "signals": {
                    "saved_tracks": int(self.db.scalar("SELECT COUNT(*) FROM spotify_saved_tracks") or 0),
                    "saved_albums": int(self.db.scalar("SELECT COUNT(*) FROM spotify_saved_albums") or 0),
                    "playlists": int(self.db.scalar("SELECT COUNT(*) FROM playlists") or 0),
                    "followed_artists": int(self.db.scalar("SELECT COUNT(*) FROM spotify_followed_artists") or 0),
                    "recently_played": int(self.db.scalar("SELECT COUNT(*) FROM spotify_recently_played") or 0),
                },
            },
            "top_artists": ranked_artists[:limit],
            "top_genres": [
                {"genre": name, "weight": round(weight, 3)}
                for name, weight in sorted(genre_weights.items(), key=lambda item: item[1], reverse=True)[:limit]
            ],
            "top_labels": [
                {"label": name, "weight": round(weight, 3)}
                for name, weight in sorted(label_weights.items(), key=lambda item: item[1], reverse=True)[:limit]
            ],
            "era_profile": [
                {"era": era, "weight": round(weight, 3)}
                for era, weight in sorted(era_weights.items(), key=lambda item: item[1], reverse=True)
            ],
            "instrumental_ratio": instrumental_ratio,
            "recent_artists": [
                item["name"]
                for item in ranked_artists
                if item["artist_id"] in recent_artists
            ][:limit],
            "playlist_overlap": [
                {"artist": artists[artist_id]["name"] if artist_id in artists else str(artist_id),
                 "playlists": count}
                for artist_id, count in sorted(playlist_overlap.items(), key=lambda item: item[1], reverse=True)
                if artist_id in weights
            ][:limit],
            "weights": {str(key): round(value, 3) for key, value in weights.items()},
            "contributions": {str(key): dict(value) for key, value in self._contributions.items()},
        }
        self.db.set_preference(PROFILE_KEY, profile)
        self.db.record_log("INFO", "taste", "Taste profile rebuilt", {"artists": len(weights)})
        return profile

    def load(self) -> dict[str, Any] | None:
        profile = self.db.get_preference(PROFILE_KEY)
        return profile if isinstance(profile, dict) else None


def profile_text(profile: dict[str, Any]) -> str:
    if not profile or profile.get("empty"):
        return profile.get("message", "No taste profile yet.") if profile else "No taste profile yet."
    lines = [
        f"Taste profile generated {profile.get('generated_at')}",
        "",
        "Top artists:",
    ]
    for item in profile.get("top_artists", [])[:15]:
        signals = ", ".join(f"{k}={v:.1f}" for k, v in item.get("signals", {}).items())
        lines.append(f"  {item['name']:<32} weight={item['weight']:<8} ({signals})")
    lines += ["", "Top genres:"]
    for item in profile.get("top_genres", [])[:15]:
        lines.append(f"  {item['genre']:<32} {item['weight']}")
    if profile.get("top_labels"):
        lines += ["", "Labels:"]
        for item in profile["top_labels"][:10]:
            lines.append(f"  {item['label']:<32} {item['weight']}")
    lines += ["", "Era preference:"]
    for item in profile.get("era_profile", []):
        lines.append(f"  {item['era']:<10} {item['weight']}")
    if profile.get("instrumental_ratio") is not None:
        lines.append(f"\nInstrumental affinity: {profile['instrumental_ratio']:.0%}")
    return "\n".join(lines)
