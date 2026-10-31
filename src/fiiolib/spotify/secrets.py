"""macOS Keychain-backed secret storage with a hardened file fallback."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

SERVICE = "fiio-library-manager"
FALLBACK_DIR = Path.home() / "Library" / "Application Support" / "fiio-library"
FALLBACK_FILE = FALLBACK_DIR / "secrets.json"


class SecretStoreError(RuntimeError):
    pass


def _keychain_available() -> bool:
    try:
        subprocess.run(["security", "-h"], capture_output=True, check=False, timeout=10)
        return True
    except (OSError, subprocess.SubprocessError):
        return False


def _keychain_set(account: str, value: str) -> bool:
    result = subprocess.run(
        [
            "security",
            "add-generic-password",
            "-a",
            account,
            "-s",
            SERVICE,
            "-w",
            value,
            "-U",
        ],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    return result.returncode == 0


def _keychain_get(account: str) -> str | None:
    result = subprocess.run(
        ["security", "find-generic-password", "-a", account, "-s", SERVICE, "-w"],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def _keychain_delete(account: str) -> None:
    subprocess.run(
        ["security", "delete-generic-password", "-a", account, "-s", SERVICE],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )


def _read_fallback() -> dict[str, str]:
    if not FALLBACK_FILE.exists():
        return {}
    try:
        return json.loads(FALLBACK_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _write_fallback(data: dict[str, str]) -> None:
    FALLBACK_DIR.mkdir(parents=True, exist_ok=True)
    FALLBACK_FILE.write_text(json.dumps(data), encoding="utf-8")
    os.chmod(FALLBACK_FILE, 0o600)


def set_secret(account: str, value: str) -> None:
    if _keychain_available() and _keychain_set(account, value):
        return
    data = _read_fallback()
    data[account] = value
    _write_fallback(data)


def get_secret(account: str) -> str | None:
    if _keychain_available():
        value = _keychain_get(account)
        if value:
            return value
    return _read_fallback().get(account)


def delete_secret(account: str) -> None:
    if _keychain_available():
        _keychain_delete(account)
    data = _read_fallback()
    if account in data:
        del data[account]
        _write_fallback(data)


def set_json(account: str, payload: dict) -> None:
    set_secret(account, json.dumps(payload))


def get_json(account: str) -> dict | None:
    raw = get_secret(account)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SecretStoreError(f"Stored secret {account!r} is not valid JSON") from exc
