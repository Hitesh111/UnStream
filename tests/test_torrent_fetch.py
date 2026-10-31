from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

lt = pytest.importorskip("libtorrent", reason="libtorrent is needed for the torrent fetcher")

from fiiolib.torrent_fetch import _human_bytes, fetch  # noqa: E402


def _torrent_file(root: Path, name: str, payload: bytes) -> Path:
    source = root / "seed"
    source.mkdir()
    (source / name).write_bytes(payload)
    storage = lt.file_storage()
    storage.add_file(name, len(payload))
    creator = lt.create_torrent(storage)
    lt.set_piece_hashes(creator, str(source))
    torrent = root / "test.torrent"
    torrent.write_bytes(lt.bencode(creator.generate()))
    return torrent


def test_fetch_from_local_peer(tmp_path: Path) -> None:
    payload = os.urandom(300_000)
    torrent = _torrent_file(tmp_path, "sample.bin", payload)
    info = lt.torrent_info(str(torrent))

    settings = {
        "listen_interfaces": "127.0.0.1:0",
        "enable_dht": False,
        "enable_lsd": False,
        "enable_upnp": False,
        "enable_natpmp": False,
    }
    seeder = lt.session(settings)
    params = lt.add_torrent_params()
    params.ti = info
    params.save_path = str(tmp_path / "seed")
    params.flags |= lt.torrent_flags.seed_mode
    handle = seeder.add_torrent(params)
    try:
        for _ in range(200):
            if handle.status().is_seeding:
                break
            time.sleep(0.02)
        assert handle.status().is_seeding

        destination = tmp_path / "dest"
        port = seeder.listen_port()
        fetch(
            str(torrent),
            destination,
            quiet=True,
            timeout=30,
            on_start=lambda h: h.connect_peer(("127.0.0.1", port)),
        )
        assert (destination / "sample.bin").read_bytes() == payload
    finally:
        seeder.pause()


def test_human_bytes() -> None:
    assert _human_bytes(0) == "0 B"
    assert _human_bytes(1500) == "1.5 KB"
    assert _human_bytes(5 * 1024**3) == "5.0 GB"
