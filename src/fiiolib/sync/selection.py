"""Collection profiles and file selection for the limited-capacity card."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from fiiolib.db import Database
from fiiolib.library.queries import files_for_collection
from fiiolib.spotify.taste import PROFILE_KEY

PROFILES = ("complete", "favourites", "discovery", "daily", "genre", "offline")


@dataclass
class Selection:
    profile: str
    files: list[dict[str, Any]]
    total_bytes: int
    notes: list[str]


def _artist_weights(db: Database) -> dict[int, float]:
    profile = db.get_preference(PROFILE_KEY) or {}
    weights: dict[int, float] = {}
    for key, value in (profile.get("weights") or {}).items():
        try:
            weights[int(key)] = float(value)
        except (TypeError, ValueError):
            continue
    return weights


def _saved_track_ids(db: Database) -> set[int]:
    return {int(row["track_id"]) for row in db.query("SELECT track_id FROM spotify_saved_tracks")}


def _track_scores(db: Database) -> dict[int, float]:
    weights = _artist_weights(db)
    saved = _saved_track_ids(db)
    recent = {
        int(row["track_id"]): row["plays"]
        for row in db.query(
            "SELECT track_id, COUNT(*) AS plays FROM spotify_recently_played GROUP BY track_id"
        )
    }
    scores: dict[int, float] = {}
    for row in db.query(
        "SELECT t.id, t.artist_id, t.album_id, t.track_number, "
        "(SELECT COUNT(*) FROM playlist_tracks pt WHERE pt.track_id = t.id) AS playlist_count "
        "FROM tracks t"
    ):
        score = weights.get(int(row["artist_id"] or 0), 0.0)
        if row["id"] in saved:
            score += 5.0
        score += min(int(recent.get(row["id"], 0)), 5) * 0.6
        score += min(int(row["playlist_count"] or 0), 5) * 0.4
        scores[int(row["id"])] = score
    return scores


def select_files(
    db: Database,
    profile: str = "complete",
    genres: list[str] | None = None,
    artist_ids: list[int] | None = None,
    album_ids: list[int] | None = None,
    available_bytes: int | None = None,
) -> Selection:
    if profile not in PROFILES:
        raise ValueError(f"Unknown profile {profile!r}; choose from {', '.join(PROFILES)}")
    notes: list[str] = []
    if profile == "genre" and not genres:
        raise ValueError("The genre profile requires at least one --genre value")

    files = files_for_collection(
        db,
        genres=genres,
        artist_ids=artist_ids,
        album_ids=album_ids,
        only_verified=True,
    )
    scores = _track_scores(db)
    for item in files:
        item["score"] = round(scores.get(int(item["track_id"]), 0.0), 3)

    if profile == "favourites":
        weights = _artist_weights(db)
        if weights:
            threshold = sorted(weights.values(), reverse=True)[max(0, len(weights) // 3 - 1)] if weights else 0
            saved = _saved_track_ids(db)
            files = [
                item
                for item in files
                if item["score"] >= threshold or int(item["track_id"]) in saved
            ]
        else:
            notes.append("No taste weights yet; favourites falls back to saved tracks only")
    elif profile == "discovery":
        recent_cutoff = db.scalar(
            "SELECT date('now', '-30 days')"
        )
        files = [
            item
            for item in files
            if item["track_id"] in _discovery_track_ids(db, recent_cutoff)
        ]
        if not files:
            notes.append("No recently organised discovery tracks yet")
    elif profile == "daily":
        files = sorted(files, key=lambda item: item["score"], reverse=True)[:250]
    elif profile == "offline":
        files = sorted(files, key=lambda item: item["score"], reverse=True)
    else:
        files = sorted(files, key=lambda item: (item.get("artist_name") or "", item.get("album_title") or "", item.get("disc_number") or 1, item.get("track_number") or 0))

    total = 0
    selected: list[dict[str, Any]] = []
    for item in files:
        size = int(item.get("size_bytes") or 0)
        if available_bytes is not None and profile in {"offline", "daily"} and total + size > available_bytes:
            continue
        if available_bytes is not None and profile == "offline" and total + size > available_bytes:
            break
        selected.append(item)
        total += size
    if available_bytes is not None and total > available_bytes and profile not in {"offline", "daily"}:
        notes.append(
            "Selection exceeds free space; use --profile offline to fit automatically or narrow filters"
        )
    return Selection(profile=profile, files=selected, total_bytes=total, notes=notes)


def _discovery_track_ids(db: Database, cutoff: str | None) -> set[int]:
    rows = db.query(
        "SELECT DISTINCT t.id FROM files f JOIN tracks t ON t.id = f.track_id "
        "WHERE f.organised_at IS NOT NULL AND (? IS NULL OR f.organised_at >= ?)",
        (cutoff, cutoff),
    )
    return {int(row["id"]) for row in rows}
