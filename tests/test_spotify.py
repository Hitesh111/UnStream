from __future__ import annotations

from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx

from fiiolib.config import Config
from fiiolib.db import Database
from fiiolib.spotify.client import SpotifyClient
from fiiolib.spotify.sync import SpotifyImporter
from fiiolib.spotify.taste import TasteProfiler, profile_text


class FakeAuth:
    def access_token(self, force_refresh: bool = False) -> str:
        return "fake-token"

    def refresh(self, tokens: dict | None = None) -> dict:
        return {}


def _artist(artist_id: str, name: str, genres: list[str] | None = None) -> dict[str, Any]:
    return {"id": artist_id, "name": name, "genres": genres or [], "images": [{"url": "http://img", "width": 300, "height": 300}]}


def _track(track_id: str, title: str, artist_id: str, artist_name: str, album_id: str, year: str = "2020-01-01") -> dict[str, Any]:
    return {
        "id": track_id,
        "name": title,
        "artists": [_artist(artist_id, artist_name)],
        "album": {
            "id": album_id,
            "name": f"Album {album_id}",
            "album_type": "album",
            "release_date": year,
            "total_tracks": 10,
            "images": [{"url": "http://cover", "width": 640, "height": 640}],
            "artists": [_artist(artist_id, artist_name)],
        },
        "disc_number": 1,
        "track_number": 1,
        "duration_ms": 300000,
        "explicit": False,
        "external_ids": {"isrc": f"ISRC{track_id}"},
        "external_urls": {"spotify": f"https://open.spotify.com/track/{track_id}"},
    }


def _page(items: list[dict], next_url: str | None = None) -> dict[str, Any]:
    return {"items": items, "next": next_url, "total": len(items), "limit": 50, "offset": 0}


class FakeSpotify:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        parsed = urlparse(str(request.url))
        path = parsed.path.replace("/v1", "")
        self.calls.append(f"{path}?{parsed.query}")
        track = _track("t1", "Neon Drift", "a1", "Aurora Machine", "al1", "1995-05-01")
        track2 = _track("t2", "Raga Dawn", "a2", "Sitar Nomad", "al2", "2019-03-01")
        body: dict[str, Any]
        if path == "/me":
            body = {"id": "me", "display_name": "Hitesh", "product": "premium"}
        elif path == "/me/tracks":
            offset = int(parse_qs(parsed.query).get("offset", ["0"])[0])
            if offset == 0:
                body = _page([{"added_at": "2024-01-01T00:00:00Z", "track": track}], "https://api.spotify.com/v1/me/tracks?offset=50&limit=50")
            else:
                body = _page([{"added_at": "2024-02-01T00:00:00Z", "track": track2}])
        elif path == "/me/albums":
            body = _page([{"added_at": "2024-01-01", "album": track["album"]}])
        elif path == "/me/playlists":
            body = _page(
                [
                    {"id": "p1", "name": "Mine", "owner": {"id": "me", "display_name": "Hitesh"}, "snapshot_id": "s1", "collaborative": False, "public": False, "items": {"total": 2}},
                    {"id": "p2", "name": "Someone else", "owner": {"id": "other", "display_name": "Other"}, "snapshot_id": "s2", "collaborative": False, "public": True, "items": {"total": 30}},
                ]
            )
        elif path == "/playlists/p1/items":
            body = _page([
                {"added_at": "2024-01-01", "added_by": {"id": "me"}, "item": track},
                {"added_at": "2024-01-02", "added_by": {"id": "me"}, "item": track2},
            ])
        elif path == "/me/following":
            body = {"artists": _page([_artist("a1", "Aurora Machine", ["deep techno", "hypnotic techno"])])}
        elif path == "/me/top/artists":
            body = _page([_artist("a1", "Aurora Machine", ["deep techno", "hypnotic techno"]), _artist("a2", "Sitar Nomad", ["indian classical", "sitar"])])
        elif path == "/me/top/tracks":
            body = _page([track, track2])
        elif path == "/me/player/recently-played":
            body = _page([{"played_at": "2026-10-01T10:00:00Z", "track": track, "context": {"uri": "spotify:playlist:p1"}}])
        else:
            return httpx.Response(404, json={"error": {"message": f"unhandled {path}"}})
        return httpx.Response(200, json=body)


def test_full_import_and_taste_profile(config: Config, db: Database) -> None:
    fake = FakeSpotify()
    http = httpx.Client(transport=httpx.MockTransport(fake))
    client = SpotifyClient(config, db, auth=FakeAuth(), http=http)  # type: ignore[arg-type]
    importer = SpotifyImporter(config, db, client)
    stats = importer.run(include_seeds=False)

    assert stats.saved_tracks == 2
    assert stats.playlists == 2
    assert stats.playlist_items == 2
    assert stats.followed_artists == 1
    assert any("Someone else" in item for item in stats.skipped_playlists)

    assert db.scalar("SELECT COUNT(*) FROM tracks") == 2
    assert db.scalar("SELECT COUNT(*) FROM artists") == 2
    assert db.scalar("SELECT COUNT(*) FROM albums") == 2
    assert db.scalar("SELECT COUNT(*) FROM playlist_tracks") == 2
    assert db.scalar("SELECT COUNT(*) FROM spotify_recently_played") == 1
    assert db.scalar("SELECT kind FROM playlists WHERE spotify_id='p1'") == "personal"
    assert db.scalar("SELECT kind FROM playlists WHERE spotify_id='p2'") == "followed"

    importer.run(include_seeds=False)
    assert db.scalar("SELECT COUNT(*) FROM tracks") == 2, "re-import must not duplicate tracks"

    profile = TasteProfiler(config, db).build()
    assert not profile.get("empty")
    assert profile["top_artists"][0]["name"] in {"Aurora Machine", "Sitar Nomad"}
    assert profile["top_genres"]
    text = profile_text(profile)
    assert "Top artists" in text
    assert db.get_preference("taste.profile")["top_artists"]


def test_client_pagination_and_search_limit(config: Config, db: Database) -> None:
    fake = FakeSpotify()
    http = httpx.Client(transport=httpx.MockTransport(fake))
    client = SpotifyClient(config, db, auth=FakeAuth(), http=http)  # type: ignore[arg-type]
    tracks = list(client.saved_tracks())
    assert len(tracks) == 2
    assert any("offset=50" in call for call in fake.calls)


def test_playlist_item_shapes() -> None:
    from fiiolib.spotify.client import normalize_playlist_item

    old = {"track": {"id": "x", "type": "track"}, "added_at": "2020"}
    new = {"item": {"id": "y", "type": "track"}, "added_at": "2021", "added_by": {"id": "me"}}
    local = {"item": {"id": "z", "is_local": True, "type": "track"}}
    assert normalize_playlist_item(old)["id"] == "x"
    assert normalize_playlist_item(new)["added_by"] == "me"
    assert normalize_playlist_item(local) is None
