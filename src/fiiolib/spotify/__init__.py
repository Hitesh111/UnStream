"""Spotify integration package."""

from fiiolib.spotify.auth import SpotifyAuth, SpotifyAuthError
from fiiolib.spotify.client import SpotifyAPIError, SpotifyClient, SpotifyUnavailable
from fiiolib.spotify.sync import SpotifyImporter
from fiiolib.spotify.taste import TasteProfiler

__all__ = [
    "SpotifyAPIError",
    "SpotifyAuth",
    "SpotifyAuthError",
    "SpotifyClient",
    "SpotifyImporter",
    "SpotifyUnavailable",
    "TasteProfiler",
]
