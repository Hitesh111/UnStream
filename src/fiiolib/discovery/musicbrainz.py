"""MusicBrainz metadata client (artist/release-group/label lookup)."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from fiiolib.discovery.http import PoliteClient

logger = logging.getLogger(__name__)

MB_BASE = "https://musicbrainz.org/ws/2"


@dataclass
class ReleaseGroup:
    mbid: str
    title: str
    primary_type: str | None
    secondary_types: list[str]
    first_release_date: str | None
    artist_credit: str | None
    score: int

    @property
    def year(self) -> int | None:
        if not self.first_release_date:
            return None
        try:
            return int(self.first_release_date[:4])
        except ValueError:
            return None


class MusicBrainzClient:
    def __init__(self, user_agent: str, min_interval: float = 1.0, cache_dir=None) -> None:
        self.http = PoliteClient(user_agent, min_interval=min_interval, cache_dir=cache_dir)

    def close(self) -> None:
        self.http.close()

    def search_artists(self, name: str, limit: int = 5) -> list[dict[str, Any]]:
        payload = self.http.get_json(
            f"{MB_BASE}/artist",
            params={"query": f'artist:"{name}"', "fmt": "json", "limit": limit},
            cache_key=f"mb_artist_{name}",
        )
        return [a for a in payload.get("artists", []) if a.get("score", 0) >= 90]

    def release_groups(self, artist_mbid: str, limit: int = 100) -> list[ReleaseGroup]:
        groups: list[ReleaseGroup] = []
        offset = 0
        while len(groups) < limit:
            payload = self.http.get_json(
                f"{MB_BASE}/release-group",
                params={
                    "artist": artist_mbid,
                    "fmt": "json",
                    "limit": min(100, limit - len(groups)),
                    "offset": offset,
                    "inc": "artist-credits",
                },
                cache_key=f"mb_rg_{artist_mbid}_{offset}",
            )
            items = payload.get("release-groups", [])
            if not items:
                break
            for item in items:
                credit = ", ".join(
                    a.get("name", "") for a in (item.get("artist-credit") or []) if isinstance(a, dict)
                )
                groups.append(
                    ReleaseGroup(
                        mbid=item.get("id", ""),
                        title=item.get("title", ""),
                        primary_type=item.get("primary-type"),
                        secondary_types=item.get("secondary-types") or [],
                        first_release_date=item.get("first-release-date"),
                        artist_credit=credit or None,
                        score=int(item.get("score", 0) or 0),
                    )
                )
            offset += len(items)
            if len(items) < 100:
                break
        return groups

    def release_group_labels(self, release_group_mbid: str) -> list[str]:
        payload = self.http.get_json(
            f"{MB_BASE}/release",
            params={"release-group": release_group_mbid, "fmt": "json", "limit": 25, "inc": "labels"},
            cache_key=f"mb_releases_{release_group_mbid}",
        )
        labels: list[str] = []
        for release in payload.get("releases", []):
            for info in release.get("label-info") or []:
                label = (info or {}).get("label") or {}
                name = label.get("name")
                if name and name not in labels:
                    labels.append(name)
        return labels

    def search_release_groups(self, query: str, limit: int = 25) -> list[ReleaseGroup]:
        payload = self.http.get_json(
            f"{MB_BASE}/release-group",
            params={"query": query, "fmt": "json", "limit": min(limit, 100)},
            cache_key=f"mb_search_rg_{query}",
        )
        return [
            ReleaseGroup(
                mbid=item.get("id", ""),
                title=item.get("title", ""),
                primary_type=item.get("primary-type"),
                secondary_types=item.get("secondary-types") or [],
                first_release_date=item.get("first-release-date"),
                artist_credit=None,
                score=int(item.get("score", 0) or 0),
            )
            for item in payload.get("release-groups", [])
        ]

    def artist_genres(self, artist_mbid: str) -> list[str]:
        payload = self.http.get_json(
            f"{MB_BASE}/artist/{artist_mbid}",
            params={"fmt": "json", "inc": "genres+tags"},
            cache_key=f"mb_artist_genres_{artist_mbid}",
        )
        genres = [g.get("name", "") for g in payload.get("genres", []) if g.get("name")]
        tags = [t.get("name", "") for t in payload.get("tags", []) if t.get("name")]
        return list(dict.fromkeys(genres + tags))
