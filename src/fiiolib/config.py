"""Configuration loading and validation."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, field_validator

DEFAULT_CONFIG_PATHS = (
    Path.home() / ".config" / "fiio-library" / "config.yaml",
    Path.cwd() / "config.yaml",
)


class AppConfig(BaseModel):
    name: str = "FiiO Library Manager"
    timezone: str = "Asia/Kolkata"
    library_root: Path = Path.home() / "FiiO-Library"
    log_level: str = "INFO"

    @field_validator("library_root", mode="before")
    @classmethod
    def _expand(cls, value: Any) -> Path:
        return Path(str(value)).expanduser().resolve()


class SpotifyConfig(BaseModel):
    enabled: bool = True
    sync_interval_hours: int = 24
    redirect_port: int = 8888
    client_id: str = ""
    scopes: list[str] = Field(
        default_factory=lambda: [
            "user-library-read",
            "user-read-private",
            "playlist-read-private",
            "playlist-read-collaborative",
            "user-follow-read",
            "user-top-read",
            "user-read-recently-played",
        ]
    )
    seed_playlists: list[str] = Field(default_factory=list)

    @property
    def redirect_uri(self) -> str:
        return f"http://127.0.0.1:{self.redirect_port}/callback"


class AudioConfig(BaseModel):
    preferred_format: str = "FLAC"
    preferred_sample_rates: list[int] = Field(default_factory=lambda: [44100, 48000, 96000])
    preferred_bit_depths: list[int] = Field(default_factory=lambda: [16, 24])
    fallback_format: str = "MP3"
    fallback_bitrate: int = 320
    ffmpeg_path: str = "ffmpeg"
    ffprobe_path: str = "ffprobe"
    flac_block_size: int = 4096
    max_channels: int = 2


class DownloadsConfig(BaseModel):
    enabled: bool = False
    approval_required: bool = True
    preferred_method: str = "authorised_torrent"
    qbittorrent_url: str = "http://127.0.0.1:8080"
    qbittorrent_username: str = "admin"
    min_free_gb: float = 2.0
    max_concurrent: int = 3


class DiscoveryConfig(BaseModel):
    musicbrainz_user_agent: str = "FiiO-Library-Manager/0.1 (personal use)"
    musicbrainz_min_interval_seconds: float = 1.0
    internet_archive_min_interval_seconds: float = 1.0
    max_candidates_per_artist: int = 5
    allow_manual_review_candidates: bool = False


class LibraryConfig(BaseModel):
    duplicate_detection: bool = True
    preserve_original_metadata: bool = True
    organize_on_verify: bool = True
    audio_extensions: list[str] = Field(
        default_factory=lambda: [
            ".flac",
            ".mp3",
            ".m4a",
            ".ogg",
            ".wav",
            ".ape",
            ".dsf",
            ".dff",
            ".wma",
        ]
    )


class DeviceConfig(BaseModel):
    expected_player: str = "SNOWSKY Echo Mini"
    expected_card_capacity_gb: int = 64
    filesystem_whitelist: list[str] = Field(default_factory=lambda: ["exfat", "msdos", "fat32", "custom"])
    mount_point: str | None = None
    auto_detect: bool = True
    auto_sync: bool = False
    dry_run_by_default: bool = True
    reserve_free_gb: float = 1.5
    music_subdir: str = "Music"
    artwork_size: int = 500
    max_flac_block_size: int = 4608


class AutomationConfig(BaseModel):
    spotify_sync: str = "daily"
    discovery: str = "daily"
    integrity_check: str = "weekly"
    metadata_refresh: str = "weekly"
    card_sync: str = "on_detect_user_enabled"


class DashboardConfig(BaseModel):
    host: str = "127.0.0.1"
    port: int = 8765


class Config(BaseModel):
    app: AppConfig = Field(default_factory=AppConfig)
    spotify: SpotifyConfig = Field(default_factory=SpotifyConfig)
    audio: AudioConfig = Field(default_factory=AudioConfig)
    downloads: DownloadsConfig = Field(default_factory=DownloadsConfig)
    discovery: DiscoveryConfig = Field(default_factory=DiscoveryConfig)
    library: LibraryConfig = Field(default_factory=LibraryConfig)
    device: DeviceConfig = Field(default_factory=DeviceConfig)
    automation: AutomationConfig = Field(default_factory=AutomationConfig)
    dashboard: DashboardConfig = Field(default_factory=DashboardConfig)

    # ---------------------------------------------------------------- paths
    @property
    def root(self) -> Path:
        return self.app.library_root

    def subdir(self, name: str) -> Path:
        return self.root / name

    @property
    def torrents_dir(self) -> Path:
        return self.subdir("torrents")

    @property
    def downloads_dir(self) -> Path:
        return self.subdir("downloads")

    @property
    def verified_dir(self) -> Path:
        return self.subdir("verified")

    @property
    def music_dir(self) -> Path:
        return self.subdir("music")

    @property
    def artwork_dir(self) -> Path:
        return self.subdir("artwork")

    @property
    def database_dir(self) -> Path:
        return self.subdir("database")

    @property
    def logs_dir(self) -> Path:
        return self.subdir("logs")

    @property
    def reports_dir(self) -> Path:
        return self.subdir("reports")

    @property
    def backups_dir(self) -> Path:
        return self.subdir("backups")

    @property
    def db_path(self) -> Path:
        return self.database_dir / "library.sqlite3"

    @property
    def config_file(self) -> Path | None:
        return self._config_file

    _config_file: Path | None = None

    def ensure_directories(self) -> None:
        for path in (
            self.torrents_dir,
            self.downloads_dir,
            self.verified_dir,
            self.music_dir,
            self.artwork_dir,
            self.database_dir,
            self.logs_dir,
            self.reports_dir,
            self.backups_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)
        for genre in (
            "Electronic/Techno",
            "Electronic/Trance",
            "Electronic/Psytrance",
            "Electronic/Goa",
            "Electronic/Progressive-House",
            "Electronic/Ambient",
            "Electronic/Downtempo",
            "Rock/Psychedelic-Rock",
            "Rock/Progressive-Rock",
            "Rock/Space-Rock",
            "Rock/Instrumental",
            "Indian/Classical",
            "Indian/Sitar-Fusion",
            "Indian/Instrumental",
            "World/Flamenco",
            "World/Latin",
            "World/Folk",
            "Experimental",
            "Soundtracks",
        ):
            (self.music_dir / genre).mkdir(parents=True, exist_ok=True)


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def _read_yaml(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise TypeError(f"Configuration at {path} must be a mapping")
    return data


def load_config(path: str | Path | None = None) -> Config:
    """Load configuration from an explicit path, env var, or the default chain."""
    candidates: list[Path] = []
    explicit = path or os.environ.get("FIOLIB_CONFIG")
    if explicit:
        candidates.append(Path(str(explicit)).expanduser())
    else:
        candidates.extend(DEFAULT_CONFIG_PATHS)

    data: dict[str, Any] = {}
    chosen: Path | None = None
    for candidate in candidates:
        if candidate.exists():
            data = _read_yaml(candidate)
            chosen = candidate
            break

    config = Config.model_validate(data)
    if os.environ.get("FIOLIB_ROOT"):
        config.app.library_root = Path(os.environ["FIOLIB_ROOT"]).expanduser().resolve()
    if os.environ.get("FIOLIB_QBIT_URL"):
        config.downloads.qbittorrent_url = os.environ["FIOLIB_QBIT_URL"]
    config._config_file = chosen
    return config


def write_default_config(path: Path, force: bool = False) -> Path:
    path = path.expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and not force:
        raise FileExistsError(f"{path} already exists")
    example = Path(__file__).resolve().parents[2] / "config.example.yaml"
    if example.exists():
        path.write_text(example.read_text(encoding="utf-8"), encoding="utf-8")
    else:
        path.write_text("app:\n  name: FiiO Library Manager\n", encoding="utf-8")
    return path
