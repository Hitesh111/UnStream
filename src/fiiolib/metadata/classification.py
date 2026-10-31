"""Canonical genre classification for the library folder structure."""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

RULES: list[tuple[tuple[str, ...], str, str]] = [
    (("psytrance", "psy-trance", "psy trance", "darkpsy", "hi-tech", "hitech", "forest psy"), "Electronic", "Psytrance"),
    (("goa",), "Electronic", "Goa"),
    (("progressive house", "prog house", "melodic house"), "Electronic", "Progressive-House"),
    (("trance",), "Electronic", "Trance"),
    (("hypnotic techno", "deep techno", "melodic techno", "minimal techno", "dub techno", "techno"), "Electronic", "Techno"),
    (("psybient", "psychill", "cinematic ambient", "dark ambient", "drone", "ambient"), "Electronic", "Ambient"),
    (("downtempo", "chillout", "chill-out", "trip hop"), "Electronic", "Downtempo"),
    (("psychedelic rock", "psychedelia", "acid rock", "krautrock", "neo-psychedelia"), "Rock", "Psychedelic-Rock"),
    (("progressive rock", "prog rock", "symphonic rock"), "Rock", "Progressive-Rock"),
    (("space rock", "stoner rock"), "Rock", "Space-Rock"),
    (("post-rock", "instrumental rock", "math rock"), "Rock", "Instrumental"),
    (("hindustani", "carnatic", "indian classical", "raga", "raga rock", "dhrupad", "khayal"), "Indian", "Classical"),
    (("sitar", "sitar fusion", "indian fusion", "indo-jazz"), "Indian", "Sitar-Fusion"),
    (("flamenco", "gipsy", "gypsy", "rumba"), "World", "Flamenco"),
    (("latin", "bossa nova", "salsa", "tango"), "World", "Latin"),
    (("folk", "world", "traditional", "celtic", "afrobeat"), "World", "Folk"),
    (("soundtrack", "film score", "score", "ost", "video game"), "Soundtracks", "Soundtracks"),
    (("experimental", "avant-garde", "avant garde", "minimalism", "musique concrète", "noise"), "Experimental", "Experimental"),
]

CATEGORY_FALLBACKS = {
    "electronic": ("Electronic", "Ambient"),
    "rock": ("Rock", "Psychedelic-Rock"),
    "indian": ("Indian", "Classical"),
    "world": ("World", "Folk"),
    "soundtrack": ("Soundtracks", "Soundtracks"),
    "experimental": ("Experimental", "Experimental"),
}

UNSORTED = ("Unsorted", "Unsorted")


@dataclass
class Classification:
    category: str
    subgenre: str
    matched_genre: str | None
    confidence: float

    @property
    def relative_dir(self) -> str:
        return f"{self.category}/{self.subgenre}"


def classify_genres(genres: Iterable[str] | None) -> Classification:
    values = [g for g in (genres or []) if g]
    for genre in values:
        lowered = _normalise(genre)
        for keywords, category, subgenre in RULES:
            if any(keyword in lowered for keyword in keywords):
                return Classification(category, subgenre, genre, 0.9)
    if values:
        lowered = _normalise(values[0])
        for key, fallback in CATEGORY_FALLBACKS.items():
            if key in lowered:
                return Classification(fallback[0], fallback[1], values[0], 0.4)
    return Classification(UNSORTED[0], UNSORTED[1], None, 0.0)


def _normalise(value: str) -> str:
    value = value.lower().strip()
    value = re.sub(r"[_/]+", " ", value)
    return re.sub(r"\s+", " ", value)
