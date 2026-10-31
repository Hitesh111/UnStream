"""Minimal BitTorrent fetcher: a magnet link or .torrent goes straight to a folder.

This is deliberately separate from the library/Spotify/qBittorrent pipeline. It
has no database, no rights checks, and no post-processing. Use it only for
content you are authorised to download.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import tempfile
import time
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

DEFAULT_METADATA_TIMEOUT = 120.0
SESSION_SETTINGS: dict[str, Any] = {
    "listen_interfaces": "0.0.0.0:0,[::]:0",
    "enable_dht": True,
    "enable_lsd": True,
    "enable_upnp": True,
    "enable_natpmp": True,
    "announce_to_all_trackers": True,
    "announce_to_all_tiers": True,
}


class TorrentError(RuntimeError):
    """A torrent could not be added, resolved, or completed."""


def _libtorrent() -> Any:
    try:
        import libtorrent as lt
    except ImportError as exc:  # pragma: no cover - depends on optional extra
        raise TorrentError(
            "libtorrent is not installed. Run: pip install 'fiio-library-manager[torrent]'"
        ) from exc
    return lt


def _human_bytes(value: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(value) < 1024 or unit == "TB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{value:.0f} B"
        value /= 1024
    return f"{value:.1f} TB"


def _download_torrent_file(url: str) -> Path:
    try:
        with urllib.request.urlopen(url, timeout=60) as response:
            payload = response.read()
    except OSError as exc:
        raise TorrentError(f"Could not download torrent file: {exc}") from exc
    descriptor, name = tempfile.mkstemp(prefix="fiiolib-", suffix=".torrent")
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(payload)
    return Path(name)


def _source_params(lt: Any, source: str) -> tuple[Any, Path | None]:
    if source.lower().startswith("magnet:"):
        try:
            return lt.parse_magnet_uri(source), None
        except RuntimeError as exc:
            raise TorrentError(f"Invalid magnet link: {exc}") from exc

    if source.lower().startswith(("http://", "https://")):
        torrent_file = _download_torrent_file(source)
        cleanup = torrent_file
    else:
        torrent_file = Path(source).expanduser()
        cleanup = None
        if not torrent_file.is_file():
            raise TorrentError(f"No such .torrent file: {torrent_file}")

    try:
        params = lt.add_torrent_params()
        params.ti = lt.torrent_info(str(torrent_file))
        return params, cleanup
    except RuntimeError as exc:
        if cleanup:
            cleanup.unlink(missing_ok=True)
        raise TorrentError(f"Invalid torrent file: {exc}") from exc


def _make_session(lt: Any) -> Any:
    settings = dict(SESSION_SETTINGS)
    settings["alert_mask"] = int(lt.alert.category_t.error_notification)
    return lt.session(settings)


def _describe(status: Any) -> str:
    if not status.has_metadata:
        return f"fetching metadata  peers={status.num_peers}"
    total = status.total_wanted or 0
    done = status.total_wanted_done or 0
    percent = (done / total * 100) if total else status.progress * 100
    return (
        f"{percent:5.1f}%  {_human_bytes(done)}/{_human_bytes(total)}  "
        f"{_human_bytes(status.download_rate)}/s  peers={status.num_peers}"
    )


def fetch(
    source: str,
    destination: str | Path,
    *,
    port: int = 0,
    metadata_timeout: float = DEFAULT_METADATA_TIMEOUT,
    timeout: float = 0.0,
    quiet: bool = False,
    on_start: Callable[[Any], None] | None = None,
) -> Path:
    """Download a magnet/.torrent into *destination* and return that folder.

    Partial data is kept on interruption; rerunning the same command rechecks
    existing pieces and continues. `on_start` receives the libtorrent handle
    (used by the local integration test to connect a peer).
    """
    lt = _libtorrent()
    dest = Path(destination).expanduser().resolve()
    dest.mkdir(parents=True, exist_ok=True)
    if not dest.is_dir():
        raise TorrentError(f"Destination is not a directory: {dest}")

    params, cleanup = _source_params(lt, source)
    params.save_path = str(dest)
    params.storage_mode = lt.storage_mode_t.storage_mode_sparse
    session = _make_session(lt)
    handle = session.add_torrent(params)
    if port:
        session.listen_on(port, port + 10)
    if on_start:
        on_start(handle)

    started = time.monotonic()
    announced = False
    try:
        while True:
            status = handle.status()
            if status.errc.value():
                raise TorrentError(f"Torrent failed: {status.errc.message()}")

            if status.has_metadata and not announced:
                info = handle.torrent_file()
                total = info.total_size()
                free = shutil.disk_usage(dest).free
                if total > free:
                    raise TorrentError(
                        f"Not enough space in {dest}: need {_human_bytes(total)}, "
                        f"have {_human_bytes(free)}"
                    )
                if not quiet:
                    print(f"Saving to {dest} ({_human_bytes(total)})", file=sys.stderr)
                announced = True

            if status.is_finished or status.is_seeding:
                break

            if not status.has_metadata:
                if metadata_timeout and time.monotonic() - started > metadata_timeout:
                    raise TorrentError(
                        f"No metadata after {metadata_timeout:.0f}s "
                        "(no peers, or the magnet has no working trackers/DHT)"
                    )
            elif timeout and time.monotonic() - started > timeout:
                raise TorrentError(
                    f"Timed out after {timeout:.0f}s at {status.progress * 100:.1f}%"
                )

            if not quiet:
                print(f"\r{_describe(status)}", end="", flush=True)
            time.sleep(0.5)
    except KeyboardInterrupt:
        if not quiet:
            print("\nInterrupted; partial data kept. Rerun to resume.", file=sys.stderr)
        raise
    finally:
        session.remove_torrent(handle)
        session.pause()
        if cleanup:
            cleanup.unlink(missing_ok=True)

    if not quiet:
        print(f"\r{' ' * 72}\r", end="")
        files = handle.torrent_file().files()
        print(f"Done: {files.num_files()} file(s) in {dest}", file=sys.stderr)
        for index in range(files.num_files()):
            print(f"  {files.file_path(index)}", file=sys.stderr)
    return dest


def run(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="torrent-fetch",
        description="Download a magnet link or .torrent into a folder. "
        "Use only for content you are authorised to download.",
    )
    parser.add_argument("source", help="magnet: URI, .torrent path, or http(s) .torrent URL")
    parser.add_argument("-d", "--dest", default=".", help="destination folder (default: current directory)")
    parser.add_argument("--port", type=int, default=0, help="listen port (default: random)")
    parser.add_argument(
        "--metadata-timeout",
        type=float,
        default=DEFAULT_METADATA_TIMEOUT,
        help="seconds to wait for magnet metadata (default: 120)",
    )
    parser.add_argument("--timeout", type=float, default=0, help="overall download timeout (0 = none)")
    parser.add_argument("-q", "--quiet", action="store_true", help="no progress output")
    args = parser.parse_args(argv)

    try:
        fetch(
            args.source,
            args.dest,
            port=args.port,
            metadata_timeout=args.metadata_timeout,
            timeout=args.timeout,
            quiet=args.quiet,
        )
    except TorrentError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(run())
