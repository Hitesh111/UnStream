"""Match audio files to database tracks using tags and fuzzy fallbacks."""

from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher
from typing import Any

from fiiolib.db import Database

PARENS = re.compile(r"[\(\[\{].*?[\)\]\}]")
FEAT = re.compile(r"\b(feat\.?|featuring|ft\.?|with)\b.*$", re.IGNORECASE)
NON_ALNUM = re.compile(r"[^a-z0-9]+")


def normalise(value: str | None) -> str:
    if not value:
        return ""
    value = unicodedata.normalize("NFKD", str(value)).encode("ascii", "ignore").decode()
    value = value.lower()
    value = PARENS.sub(" ", value)
    value = FEAT.sub(" ", value)
    value = NON_ALNUM.sub(" ", value)
    return re.sub(r"\s+", " ", value).strip()


def similarity(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    return SequenceMatcher(None, a, b).ratio()


class TrackMatcher:
    def __init__(self, db: Database) -> None:
        self.db = db
        self._index: list[dict[str, Any]] | None = None

    def _load_index(self) -> list[dict[str, Any]]:
        if self._index is not None:
            return self._index
        rows = self.db.query(
            "SELECT t.id, t.title, t.isrc, t.duration_ms, t.track_number, t.disc_number, "
            "ar.name AS artist, al.title AS album "
            "FROM tracks t LEFT JOIN artists ar ON ar.id = t.artist_id "
            "LEFT JOIN albums al ON al.id = t.album_id"
        )
        self._index = []
        for row in rows:
            item = dict(row)
            item["_title"] = normalise(item["title"])
            item["_artist"] = normalise(item["artist"])
            item["_album"] = normalise(item["album"])
            self._index.append(item)
        return self._index

    def identify(
        self,
        tags: dict[str, Any],
        duration_ms: int | None = None,
        artist_hint: str | None = None,
        album_hint: str | None = None,
    ) -> tuple[int, float] | None:
        isrc = str(tags.get("isrc") or "").strip().upper()
        if isrc:
            row = self.db.one("SELECT id FROM tracks WHERE UPPER(isrc) = ?", (isrc,))
            if row:
                return int(row["id"]), 1.0

        title = normalise(tags.get("title") or tags.get("_filename") or "")
        artist = normalise(tags.get("artist") or artist_hint or "")
        album = normalise(tags.get("album") or album_hint or "")
        if not title:
            return None

        candidates = self._load_index()
        best: tuple[int, float] | None = None
        for item in candidates:
            score = similarity(title, item["_title"]) * 0.6
            if artist:
                score += similarity(artist, item["_artist"]) * 0.3
            if album:
                score += similarity(album, item["_album"]) * 0.1
            if duration_ms and item.get("duration_ms"):
                delta = abs(int(item["duration_ms"]) - int(duration_ms))
                if delta <= 1500:
                    score += 0.1
                elif delta > 20000:
                    score -= 0.2
            if item.get("track_number") and tags.get("tracknumber"):
                try:
                    if int(item["track_number"]) == int(str(tags["tracknumber"]).split("/")[0]):
                        score += 0.05
                except (TypeError, ValueError):
                    pass
            if best is None or score > best[1]:
                best = (int(item["id"]), score)
        if best and best[1] >= 0.72:
            return best
        return None
