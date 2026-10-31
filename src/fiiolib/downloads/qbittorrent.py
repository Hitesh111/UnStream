"""qBittorrent Web API client (local, authenticated)."""

from __future__ import annotations

import logging
from typing import Any

import httpx

from fiiolib.config import Config
from fiiolib.spotify import secrets as secret_store

logger = logging.getLogger(__name__)

QBIT_PASSWORD_ACCOUNT = "qbittorrent.password"
API = "/api/v2"


class QBittorrentError(RuntimeError):
    pass


class QBittorrentClient:
    def __init__(self, config: Config, http: httpx.Client | None = None) -> None:
        self.config = config
        self.base_url = config.downloads.qbittorrent_url.rstrip("/")
        self.http = http or httpx.Client(timeout=30, follow_redirects=True)
        self._owns_http = http is None
        self._logged_in = False

    def close(self) -> None:
        if self._owns_http:
            self.http.close()

    # ------------------------------------------------------------------ auth
    def _password(self) -> str | None:
        return secret_store.get_secret(QBIT_PASSWORD_ACCOUNT)

    def set_password(self, password: str) -> None:
        secret_store.set_secret(QBIT_PASSWORD_ACCOUNT, password)

    def login(self) -> None:
        password = self._password()
        if password is None:
            raise QBittorrentError(
                "No qBittorrent password stored. Run: musicctl download setup --password"
            )
        response = self.http.post(
            f"{self.base_url}{API}/auth/login",
            data={"username": self.config.downloads.qbittorrent_username, "password": password},
        )
        if response.status_code != 200 or response.text.strip() != "Ok.":
            raise QBittorrentError(
                f"qBittorrent login failed ({response.status_code}). Check the Web UI is enabled "
                f"and the username/password are correct."
            )
        self._logged_in = True

    def _ensure_login(self) -> None:
        if not self._logged_in:
            self.login()

    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        self._ensure_login()
        response = self.http.request(method, f"{self.base_url}{API}{path}", **kwargs)
        if response.status_code == 403:
            self._logged_in = False
            self._ensure_login()
            response = self.http.request(method, f"{self.base_url}{API}{path}", **kwargs)
        return response

    # -------------------------------------------------------------- endpoints
    def version(self) -> str:
        response = self._request("GET", "/app/version")
        response.raise_for_status()
        return response.text.strip()

    def is_available(self) -> tuple[bool, str]:
        try:
            version = self.version()
            return True, version
        except (httpx.HTTPError, QBittorrentError) as exc:
            return False, str(exc)

    def free_space(self) -> int:
        response = self._request("GET", "/sync/maindata")
        response.raise_for_status()
        payload = response.json()
        return int((payload.get("server_state") or {}).get("free_space_on_disk") or 0)

    def add_torrent(
        self,
        url: str | None = None,
        torrent_bytes: bytes | None = None,
        save_path: str | None = None,
        category: str = "fiiolib",
        tags: str | None = None,
        paused: bool = False,
    ) -> None:
        data: dict[str, Any] = {
            "savepath": save_path or str(self.config.downloads_dir),
            "category": category,
            "paused": "true" if paused else "false",
        }
        if tags:
            data["tags"] = tags
        files = None
        if torrent_bytes is not None:
            files = {"torrents": ("release.torrent", torrent_bytes, "application/x-bittorrent")}
        elif url:
            data["urls"] = url
        else:
            raise QBittorrentError("Either a magnet/torrent URL or torrent bytes are required")

        response = self._request("POST", "/torrents/add", data=data, files=files)
        if response.status_code != 200:
            raise QBittorrentError(
                f"qBittorrent rejected the torrent ({response.status_code}): {response.text[:200]}"
            )

    def torrents(self, category: str | None = None, hashes: list[str] | None = None) -> list[dict[str, Any]]:
        params: dict[str, Any] = {}
        if category:
            params["category"] = category
        if hashes:
            params["hashes"] = "|".join(hashes)
        response = self._request("GET", "/torrents/info", params=params)
        response.raise_for_status()
        return response.json()

    def torrent_files(self, torrent_hash: str) -> list[dict[str, Any]]:
        response = self._request("GET", "/torrents/files", params={"hash": torrent_hash})
        response.raise_for_status()
        return response.json()

    def pause(self, hashes: list[str]) -> None:
        self._request("POST", "/torrents/pause", data={"hashes": "|".join(hashes)})

    def resume(self, hashes: list[str]) -> None:
        self._request("POST", "/torrents/resume", data={"hashes": "|".join(hashes)})

    def delete(self, hashes: list[str], delete_files: bool = False) -> None:
        self._request(
            "POST",
            "/torrents/delete",
            data={"hashes": "|".join(hashes), "deleteFiles": "true" if delete_files else "false"},
        )

    def fetch_torrent_file(self, url: str) -> bytes:
        response = httpx.get(url, follow_redirects=True, timeout=60)
        if response.status_code != 200:
            raise QBittorrentError(f"Could not download torrent file ({response.status_code})")
        return response.content


def torrent_hash_from_info(info: dict[str, Any]) -> str | None:
    value = info.get("hash") or info.get("infohash_v1")
    return str(value) if value else None
