"""Polite shared HTTP helper for public metadata services."""

from __future__ import annotations

import contextlib
import json
import logging
import threading
import time
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger(__name__)


class RateLimiter:
    def __init__(self, min_interval: float) -> None:
        self.min_interval = max(min_interval, 0.0)
        self._lock = threading.Lock()
        self._last = 0.0

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            delay = self.min_interval - (now - self._last)
            if delay > 0:
                time.sleep(delay)
            self._last = time.monotonic()


class PoliteClient:
    def __init__(
        self,
        user_agent: str,
        min_interval: float = 1.0,
        timeout: float = 30.0,
        cache_dir: Path | None = None,
    ) -> None:
        self.limiter = RateLimiter(min_interval)
        self.http = httpx.Client(
            timeout=timeout,
            headers={"User-Agent": user_agent, "Accept": "application/json"},
            follow_redirects=True,
        )
        self.cache_dir = cache_dir
        if cache_dir:
            cache_dir.mkdir(parents=True, exist_ok=True)

    def close(self) -> None:
        self.http.close()

    def get_json(
        self,
        url: str,
        params: dict[str, Any] | None = None,
        cache_key: str | None = None,
        max_age_seconds: int = 7 * 24 * 3600,
    ) -> dict[str, Any]:
        cache_file = None
        if self.cache_dir and cache_key:
            safe = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in cache_key)[:120]
            cache_file = self.cache_dir / f"{safe}.json"
            if cache_file.exists() and (time.time() - cache_file.stat().st_mtime) < max_age_seconds:
                try:
                    return json.loads(cache_file.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    pass

        self.limiter.wait()
        response = self.http.get(url, params=params)
        if response.status_code == 404:
            return {}
        if response.status_code == 503:
            time.sleep(3)
            response = self.http.get(url, params=params)
        response.raise_for_status()
        payload = response.json()
        if cache_file is not None:
            with contextlib.suppress(OSError):
                cache_file.write_text(json.dumps(payload), encoding="utf-8")
        return payload
