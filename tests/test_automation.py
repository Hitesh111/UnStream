"""Automation schedule parsing and dashboard smoke tests."""

from __future__ import annotations

from datetime import timedelta

import pytest

from fiiolib.automation.scheduler import Scheduler, SchedulerError, parse_schedule
from fiiolib.config import Config
from fiiolib.db import Database


def test_parse_schedule() -> None:
    assert parse_schedule("daily") == timedelta(days=1)
    assert parse_schedule("weekly") == timedelta(days=7)
    assert parse_schedule("30m") == timedelta(minutes=30)
    assert parse_schedule("6h") == timedelta(hours=6)
    assert parse_schedule("2d") == timedelta(days=2)
    assert parse_schedule("manual") is None
    assert parse_schedule("on_detect_user_enabled") is None
    with pytest.raises(SchedulerError):
        parse_schedule("fortnightly")


def test_due_jobs_use_workflow_history(config: Config, db: Database) -> None:
    config.automation.spotify_sync = "daily"
    config.automation.discovery = "manual"
    scheduler = Scheduler(config, db)
    assert scheduler.is_due("spotify_sync")
    assert not scheduler.is_due("discovery")

    run_id = db.start_workflow("spotify_sync")
    db.finish_workflow(run_id, "completed", {"ok": True})
    assert not scheduler.is_due("spotify_sync")

    db.execute("UPDATE workflow_runs SET finished_at = '2000-01-01T00:00:00Z' WHERE id = ?", (run_id,))
    assert scheduler.is_due("spotify_sync")


def test_dashboard_endpoints(config: Config, db: Database) -> None:
    from fastapi.testclient import TestClient

    from fiiolib.dashboard import create_app

    client = TestClient(create_app(config, db), client=("127.0.0.1", 50000))
    headers = {"host": "127.0.0.1:8765"}

    assert client.get("/api/health", headers=headers).json()["ok"] is True
    library = client.get("/api/library", headers=headers)
    assert library.status_code == 200 and library.json()["files"] == 0
    status = client.get("/api/status", headers=headers)
    assert status.status_code == 200
    assert "devices" in status.json()

    # Playlists and songs browser endpoints
    pl_res = client.get("/api/playlists", headers=headers)
    assert pl_res.status_code == 200
    assert "playlists" in pl_res.json()

    songs_res = client.get("/api/songs", headers=headers)
    assert songs_res.status_code == 200
    assert "songs" in songs_res.json()
    assert "total" in songs_res.json()

    # Activity monitor endpoints
    act_res = client.get("/api/activity", headers=headers)
    assert act_res.status_code == 200
    assert "logs" in act_res.json()
    assert "active_operation" in act_res.json()
    assert "failed_tracks" in act_res.json()

    assert client.post("/api/actions/activity-clear", headers=headers).status_code == 200
    assert client.post("/api/actions/activity-clear-failures", headers=headers).status_code == 200

    assert client.get("/", headers=headers).status_code == 200

    # Downloaded files endpoint
    dl_res = client.get("/api/downloaded-files", headers=headers)
    assert dl_res.status_code == 200
    assert "files" in dl_res.json()
    assert "total_bytes" in dl_res.json()

    # Export playlists endpoint
    exp_res = client.post("/api/actions/export-playlists", headers=headers)
    assert exp_res.status_code == 200
    assert exp_res.json()["ok"] is True

    # Batch playlist actions
    batch_res = client.post("/api/actions/download-playlists-batch?playlist_ids=1,2", headers=headers)
    assert batch_res.status_code == 200
    assert batch_res.json()["ok"] is True

    blocked = client.get("/api/health", headers={"host": "evil.example"})
    assert blocked.status_code == 403

