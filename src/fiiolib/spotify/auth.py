"""Spotify OAuth 2.0 Authorization Code with PKCE and loopback redirect."""

from __future__ import annotations

import base64
import contextlib
import hashlib
import logging
import secrets
import threading
import time
import webbrowser
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

from fiiolib.config import Config
from fiiolib.db import Database
from fiiolib.spotify import secrets as secret_store

logger = logging.getLogger(__name__)

AUTHORIZE_URL = "https://accounts.spotify.com/authorize"
TOKEN_URL = "https://accounts.spotify.com/api/token"
TOKEN_ACCOUNT = "spotify.tokens"
CLIENT_ID_SETTING = "spotify.client_id"

SETUP_HELP = """\
Spotify application setup (one time):
  1. Open https://developer.spotify.com/dashboard and create an app.
  2. Add this exact Redirect URI: http://127.0.0.1:{port}/callback
  3. Copy the Client ID (PKCE is used; no client secret is needed or stored).
  4. Run: musicctl spotify login --client-id <CLIENT_ID>

Notes from the February 2026 Web API rules:
  * The Spotify account that owns the app must have an active Premium subscription.
  * Development-mode apps are limited to 1 Client ID and 5 users.
  * Some content is only readable when you own or collaborate on the playlist.
"""


class SpotifyAuthError(RuntimeError):
    pass


@dataclass
class _CallbackResult:
    code: str | None = None
    state: str | None = None
    error: str | None = None


class _CallbackServer:
    def __init__(self, port: int) -> None:
        self.port = port
        self.result = _CallbackResult()
        self.event = threading.Event()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                parsed = urlparse(self.path)
                if parsed.path != "/callback":
                    self.send_response(404)
                    self.end_headers()
                    return
                params = parse_qs(parsed.query)
                outer.result.code = params.get("code", [None])[0]
                outer.result.state = params.get("state", [None])[0]
                outer.result.error = params.get("error", [None])[0]
                body = (
                    "<html><body style='font-family:-apple-system;padding:3rem;text-align:center'>"
                    "<h2>FiiO Library Manager</h2>"
                    f"<p>{'Authentication failed' if outer.result.error else 'Authentication received. You can close this window.'}</p>"
                    "</body></html>"
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                outer.event.set()

            def log_message(self, *args: Any) -> None:
                return

        self.httpd = HTTPServer(("127.0.0.1", port), Handler)

    def serve(self) -> None:
        self.httpd.handle_request()

    def close(self) -> None:
        self.httpd.server_close()


class SpotifyAuth:
    def __init__(self, config: Config, db: Database) -> None:
        self.config = config
        self.db = db

    # ------------------------------------------------------------- client id
    def client_id(self) -> str:
        value = self.db.get_setting(CLIENT_ID_SETTING) or self.config.spotify.client_id
        if not value:
            raise SpotifyAuthError(
                "No Spotify Client ID configured.\n\n"
                + SETUP_HELP.format(port=self.config.spotify.redirect_port)
            )
        return value

    def set_client_id(self, client_id: str) -> None:
        client_id = client_id.strip()
        if len(client_id) < 16 or " " in client_id:
            raise SpotifyAuthError("That does not look like a valid Spotify Client ID")
        self.db.set_setting(CLIENT_ID_SETTING, client_id)

    # ---------------------------------------------------------------- tokens
    def tokens(self) -> dict[str, Any] | None:
        return secret_store.get_json(TOKEN_ACCOUNT)

    def is_logged_in(self) -> bool:
        return bool(self.tokens())

    def logout(self) -> None:
        secret_store.delete_secret(TOKEN_ACCOUNT)

    def _store(self, payload: dict[str, Any], previous: dict[str, Any] | None = None) -> dict[str, Any]:
        tokens = dict(previous or {})
        tokens.update(payload)
        tokens["expires_at"] = int(time.time()) + int(payload.get("expires_in", 3600)) - 60
        tokens["obtained_at"] = int(time.time())
        secret_store.set_json(TOKEN_ACCOUNT, tokens)
        return tokens

    def access_token(self, force_refresh: bool = False) -> str:
        tokens = self.tokens()
        if not tokens:
            raise SpotifyAuthError("Not logged in. Run: musicctl spotify login")
        if force_refresh or int(tokens.get("expires_at", 0)) <= int(time.time()):
            tokens = self.refresh(tokens)
        return str(tokens["access_token"])

    def refresh(self, tokens: dict[str, Any] | None = None) -> dict[str, Any]:
        tokens = tokens or self.tokens()
        if not tokens or not tokens.get("refresh_token"):
            raise SpotifyAuthError("No refresh token available. Run: musicctl spotify login")
        response = httpx.post(
            TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "refresh_token": tokens["refresh_token"],
                "client_id": self.client_id(),
            },
            timeout=30,
        )
        if response.status_code != 200:
            raise SpotifyAuthError(
                f"Spotify token refresh failed ({response.status_code}): {response.text[:200]}"
            )
        return self._store(response.json(), previous=tokens)

    # ------------------------------------------------------------ interactive
    def login(self, open_browser: bool = True, timeout: int = 300) -> dict[str, Any]:
        client_id = self.client_id()
        verifier = secrets.token_urlsafe(64)
        challenge = base64.urlsafe_b64encode(
            hashlib.sha256(verifier.encode()).digest()
        ).decode().rstrip("=")
        state = secrets.token_urlsafe(24)
        port = self.config.spotify.redirect_port
        redirect_uri = self.config.spotify.redirect_uri

        server: _CallbackServer | None = None
        try:
            server = _CallbackServer(port)
        except OSError as exc:
            raise SpotifyAuthError(
                f"Cannot listen on 127.0.0.1:{port} ({exc}). "
                "Change spotify.redirect_port and the redirect URI in the Spotify dashboard."
            ) from exc

        query = urlencode(
            {
                "client_id": client_id,
                "response_type": "code",
                "redirect_uri": redirect_uri,
                "scope": " ".join(self.config.spotify.scopes),
                "state": state,
                "code_challenge_method": "S256",
                "code_challenge": challenge,
                "show_dialog": "true",
            }
        )
        url = f"{AUTHORIZE_URL}?{query}"

        thread = threading.Thread(target=server.serve, daemon=True)
        thread.start()
        print(f"\nAuthorise this application in your browser:\n\n{url}\n")
        if open_browser:
            with contextlib.suppress(webbrowser.Error):
                webbrowser.open(url)
        if not server.event.wait(timeout=timeout):
            server.close()
            raise SpotifyAuthError("Timed out waiting for the Spotify callback")
        server.close()

        result = server.result
        if result.error:
            raise SpotifyAuthError(f"Spotify authorisation was denied: {result.error}")
        if not result.code:
            raise SpotifyAuthError("Spotify did not return an authorization code")
        if result.state != state:
            raise SpotifyAuthError("OAuth state mismatch; aborting to protect against CSRF")

        response = httpx.post(
            TOKEN_URL,
            data={
                "grant_type": "authorization_code",
                "code": result.code,
                "redirect_uri": redirect_uri,
                "client_id": client_id,
                "code_verifier": verifier,
            },
            timeout=30,
        )
        if response.status_code != 200:
            raise SpotifyAuthError(
                f"Spotify token exchange failed ({response.status_code}): {response.text[:300]}"
            )
        payload = response.json()
        payload.setdefault("client_id", client_id)
        return self._store(payload)
