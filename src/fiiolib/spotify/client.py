"""Rate-limit aware Spotify Web API client (February 2026 contract)."""

from __future__ import annotations

import logging
import time
from collections.abc import Iterator
from typing import Any, Self

import httpx

from fiiolib.config import Config
from fiiolib.db import Database
from fiiolib.spotify.auth import SpotifyAuth

logger = logging.getLogger(__name__)

API_BASE = "https://api.spotify.com/v1"


class SpotifyAPIError(RuntimeError):
    def __init__(self, status: int, message: str, path: str) -> None:
        super().__init__(f"Spotify API {status} on {path}: {message}")
        self.status = status
        self.path = path


class SpotifyUnavailable(SpotifyAPIError):
    """Endpoint is not available to this application (dev-mode restrictions)."""


class SpotifyClient:
    def __init__(
        self,
        config: Config,
        db: Database,
        auth: SpotifyAuth | None = None,
        http: httpx.Client | None = None,
    ) -> None:
        self.config = config
        self.db = db
        self.auth = auth or SpotifyAuth(config, db)
        self.http = http or httpx.Client(timeout=30)
        self._owns_http = http is None
        self._me: dict[str, Any] | None = None

    def close(self) -> None:
        if self._owns_http:
            self.http.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ------------------------------------------------------------------ core
    def _request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        retries: int = 4,
        allow_unavailable: bool = False,
    ) -> dict[str, Any]:
        url = path if path.startswith("http") else f"{API_BASE}{path}"
        last_error: Exception | None = None
        for attempt in range(retries + 1):
            headers = {"Authorization": f"Bearer {self.auth.access_token()}"}
            try:
                response = self.http.request(method, url, params=params, headers=headers)
            except httpx.HTTPError as exc:
                last_error = exc
                if attempt == retries:
                    raise SpotifyAPIError(0, str(exc), path) from exc
                time.sleep(min(2 ** attempt, 10))
                continue

            if response.status_code == 401 and attempt == 0:
                self.auth.refresh()
                continue
            if response.status_code == 429:
                retry_after = int(response.headers.get("Retry-After", "2"))
                logger.warning("Spotify rate limit hit; sleeping %ss", retry_after)
                time.sleep(retry_after)
                continue
            if 500 <= response.status_code < 600:
                if attempt == retries:
                    raise SpotifyAPIError(response.status_code, response.text[:300], path)
                time.sleep(min(2 ** attempt, 15))
                continue
            if response.status_code in (403, 404, 410):
                message = _error_message(response)
                if allow_unavailable:
                    logger.info("Spotify endpoint unavailable (%s): %s", response.status_code, path)
                    return {}
                raise SpotifyUnavailable(response.status_code, message, path)
            if response.status_code >= 400:
                raise SpotifyAPIError(response.status_code, _error_message(response), path)
            if response.status_code == 204 or not response.content:
                return {}
            return response.json()
        raise SpotifyAPIError(0, f"Request failed: {last_error}", path)

    def get(self, path: str, params: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
        return self._request("GET", path, params=params, **kwargs)

    def paginate(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        items_key: str = "items",
        max_items: int | None = None,
        allow_unavailable: bool = False,
    ) -> Iterator[dict[str, Any]]:
        params = dict(params or {})
        params.setdefault("limit", 50)
        url: str | None = path
        emitted = 0
        while url:
            page = self._request(
                "GET", url, params=params if url == path else None, allow_unavailable=allow_unavailable
            )
            if not page:
                return
            for item in page.get(items_key) or []:
                yield item
                emitted += 1
                if max_items is not None and emitted >= max_items:
                    return
            url = page.get("next")
            if not url:
                return

    # ------------------------------------------------------------- endpoints
    def me(self, refresh: bool = False) -> dict[str, Any]:
        if self._me is None or refresh:
            self._me = self.get("/me")
        return self._me

    def saved_tracks(self, max_items: int | None = None) -> Iterator[dict[str, Any]]:
        yield from self.paginate("/me/tracks", max_items=max_items)

    def saved_albums(self, max_items: int | None = None) -> Iterator[dict[str, Any]]:
        yield from self.paginate("/me/albums", max_items=max_items)

    def playlists(self, max_items: int | None = None) -> Iterator[dict[str, Any]]:
        yield from self.paginate("/me/playlists", max_items=max_items)

    def playlist_items(
        self, playlist_id: str, max_items: int | None = None
    ) -> list[dict[str, Any]]:
        items = list(
            self.paginate(
                f"/playlists/{playlist_id}/items",
                items_key="items",
                max_items=max_items,
                allow_unavailable=True,
            )
        )
        return items

    def followed_artists(self, max_items: int | None = None) -> Iterator[dict[str, Any]]:
        url: str | None = "/me/following"
        params: dict[str, Any] | None = {"type": "artist", "limit": 50}
        emitted = 0
        while url:
            page = self.get(url, params=params, allow_unavailable=True)
            if not page:
                return
            artists = page.get("artists") or {}
            for artist in artists.get("items") or []:
                yield artist
                emitted += 1
                if max_items is not None and emitted >= max_items:
                    return
            url = artists.get("next")
            params = None

    def top_artists(self, time_range: str = "medium_term", limit: int = 50) -> list[dict[str, Any]]:
        page = self.get("/me/top/artists", params={"time_range": time_range, "limit": limit})
        return page.get("items", [])

    def top_tracks(self, time_range: str = "medium_term", limit: int = 50) -> list[dict[str, Any]]:
        page = self.get("/me/top/tracks", params={"time_range": time_range, "limit": limit})
        return page.get("items", [])

    def recently_played(self, limit: int = 50) -> list[dict[str, Any]]:
        page = self.get("/me/player/recently-played", params={"limit": min(limit, 50)})
        return page.get("items", [])

    def artist(self, artist_id: str) -> dict[str, Any]:
        return self.get(f"/artists/{artist_id}")

    def artist_albums(self, artist_id: str, limit: int = 50) -> list[dict[str, Any]]:
        return list(
            self.paginate(
                f"/artists/{artist_id}/albums",
                params={
                    "include_groups": "album,single,compilation",
                    "limit": min(limit, 50),
                },
                max_items=limit,
                allow_unavailable=True,
            )
        )

    def related_artists(self, artist_id: str) -> list[dict[str, Any]]:
        page = self.get(
            f"/artists/{artist_id}/related-artists", allow_unavailable=True
        )
        return page.get("artists", []) if page else []

    def album(self, album_id: str) -> dict[str, Any]:
        return self.get(f"/albums/{album_id}")

    def search(self, query: str, types: tuple[str, ...] = ("artist",), limit: int = 10) -> dict[str, Any]:
        return self.get(
            "/search",
            params={"q": query, "type": ",".join(types), "limit": min(limit, 10)},
        )


def _error_message(response: httpx.Response) -> str:
    try:
        payload = response.json()
        return str(payload.get("error", {}).get("message", payload))
    except (ValueError, AttributeError):
        return response.text[:300]


def normalize_playlist_item(item: dict[str, Any]) -> dict[str, Any] | None:
    """Accept both pre- and post-Feb-2026 playlist item shapes."""
    track = item.get("item") or item.get("track")
    if not track or track.get("is_local"):
        return None
    if track.get("type") not in (None, "track"):
        return None
    track["added_at"] = item.get("added_at")
    added_by = item.get("added_by") or {}
    track["added_by"] = added_by.get("id") if isinstance(added_by, dict) else None
    return track


def primary_artist(track: dict[str, Any]) -> dict[str, Any]:
    artists = track.get("artists") or []
    return artists[0] if artists else {"name": "Unknown Artist", "id": None}
