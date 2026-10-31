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
    assert client.get("/", headers=headers).status_code == 200

    blocked = client.get("/api/health", headers={"host": "evil.example"})
    assert blocked.status_code == 403
