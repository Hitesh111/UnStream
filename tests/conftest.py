"""Shared test fixtures."""

from __future__ import annotations

from pathlib import Path

import pytest

from fiiolib.config import Config
from fiiolib.db import Database


@pytest.fixture()
def config(tmp_path: Path) -> Config:
    cfg = Config()
    cfg.app.library_root = tmp_path / "FiiO-Library"
    cfg.downloads.enabled = True
    cfg.downloads.approval_required = True
    cfg.spotify.seed_playlists = []
    cfg.ensure_directories()
    return cfg


@pytest.fixture()
def db(config: Config) -> Database:
    database = Database(config.db_path)
    database.migrate()
    yield database
    database.close()
