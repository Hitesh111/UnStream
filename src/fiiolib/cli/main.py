"""`musicctl` command line interface."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from rich.json import JSON
from rich.panel import Panel
from rich.table import Table

from fiiolib.audio.probe import find_tool
from fiiolib.audio.validate import validate_audio
from fiiolib.automation.scheduler import JOB_NAMES, Scheduler, SchedulerError
from fiiolib.config import Config, load_config, write_default_config
from fiiolib.db import Database, utcnow
from fiiolib.device.detect import DeviceError, DeviceLocator, device_identifier
from fiiolib.discovery.engine import DiscoveryEngine, candidate_rows
from fiiolib.downloads.manager import AcquisitionBlocked, DownloadManager
from fiiolib.downloads.qbittorrent import QBittorrentClient, QBittorrentError
from fiiolib.library.dedupe import Deduplicator
from fiiolib.library.organiser import Organiser
from fiiolib.library.queries import library_stats
from fiiolib.library.reports import write_report
from fiiolib.library.scanner import LibraryScanner
from fiiolib.logging_setup import configure_logging
from fiiolib.spotify.auth import SETUP_HELP, SpotifyAuth, SpotifyAuthError
from fiiolib.spotify.client import SpotifyClient, SpotifyUnavailable
from fiiolib.spotify.sync import SpotifyImporter, detect_changes, status_summary
from fiiolib.spotify.taste import PROFILE_KEY, TasteProfiler, profile_text
from fiiolib.sync.planner import SyncPlanner

APP_VERSION = "0.1.0"

console = Console()
err_console = Console(stderr=True)

app = typer.Typer(
    help="FiiO Library Manager: Spotify discovery, authorised acquisition, and Echo Mini sync.",
    no_args_is_help=True,
    add_completion=False,
)
spotify_app = typer.Typer(help="Spotify connection and import.", no_args_is_help=True)
queue_app = typer.Typer(help="Approval queue and qBittorrent downloads.", no_args_is_help=True)
downloads_app = typer.Typer(help="Download status.", no_args_is_help=True)
device_app = typer.Typer(help="Card detection and confirmation.", no_args_is_help=True)
metadata_app = typer.Typer(help="Metadata refresh.", no_args_is_help=True)
automation_app = typer.Typer(help="Scheduled jobs and launchd agent.", no_args_is_help=True)
app.add_typer(spotify_app, name="spotify")
app.add_typer(queue_app, name="queue")
app.add_typer(downloads_app, name="downloads")
app.add_typer(device_app, name="device")
app.add_typer(metadata_app, name="metadata")
app.add_typer(automation_app, name="automation")


class State:
    def __init__(self, config_path: Path | None, as_json: bool, verbose: bool) -> None:
        self.config_path = config_path
        self.as_json = as_json
        self.verbose = verbose
        self._config: Config | None = None
        self._db: Database | None = None

    @property
    def config(self) -> Config:
        if self._config is None:
            self._config = load_config(self.config_path)
            self._config.ensure_directories()
            configure_logging(self._config.app.log_level, self._config.logs_dir, console=self.verbose)
        return self._config

    @property
    def db(self) -> Database:
        if self._db is None:
            self._db = Database(self.config.db_path)
            self._db.migrate()
        return self._db

    def emit(self, payload: Any, render: Any = None) -> None:
        if self.as_json:
            console.print(JSON.from_data(payload, indent=2))
        elif render is not None:
            render(payload)
        else:
            console.print(payload)


def get_state(ctx: typer.Context) -> State:
    return ctx.obj  # type: ignore[return-value]


def fail(message: str, code: int = 1) -> None:
    err_console.print(f"[red]✗[/red] {message}")
    raise typer.Exit(code)


def ok(message: str) -> None:
    console.print(f"[green]✓[/green] {message}")


def table(title: str, columns: list[str], rows: list[list[Any]]) -> None:
    render_table = Table(title=title, header_style="bold cyan", title_justify="left")
    for column in columns:
        render_table.add_column(column, overflow="fold")
    for row in rows:
        render_table.add_row(*["" if value is None else str(value) for value in row])
    console.print(render_table)


@app.callback(invoke_without_command=True)
def main(
    ctx: typer.Context,
    config: Path = typer.Option(None, "--config", "-c", help="Path to config.yaml"),
    json_output: bool = typer.Option(False, "--json", help="Machine-readable output"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Verbose console logging"),
    version: bool = typer.Option(False, "--version", help="Show version and exit"),
) -> None:
    if version:
        console.print(f"musicctl {APP_VERSION}")
        raise typer.Exit()
    ctx.obj = State(config, json_output, verbose)


# ------------------------------------------------------------------ basics
@app.command()
def init(
    ctx: typer.Context,
    force: bool = typer.Option(False, "--force", help="Overwrite an existing config file"),
) -> None:
    """Create configuration, directories, and the SQLite database."""
    state = get_state(ctx)
    target = Path(
        state.config_path
        or os.environ.get("FIOLIB_CONFIG")
        or (Path.home() / ".config" / "fiio-library" / "config.yaml")
    ).expanduser()
    try:
        path = write_default_config(target, force=force)
        ok(f"Wrote configuration to {path}")
    except FileExistsError:
        ok(f"Configuration already exists at {target.expanduser()}")
    config = state.config
    config.ensure_directories()
    state.db.migrate()
    ok(f"Library root: {config.root}")
    ok(f"Database:    {config.db_path}")
    console.print(
        Panel(
            "Next steps:\n"
            "  1. Create a Spotify app and set the redirect URI: "
            f"{config.spotify.redirect_uri}\n"
            "  2. musicctl spotify login --client-id <CLIENT_ID>\n"
            "  3. musicctl spotify sync\n"
            "  4. musicctl discover\n"
            "  5. musicctl device inspect          # confirm the microSD card\n"
            "  6. musicctl sync --dry-run",
            title="Getting started",
            border_style="cyan",
        )
    )


@app.command()
def status(ctx: typer.Context) -> None:
    """Show library, Spotify, device, and pipeline status."""
    state = get_state(ctx)
    db = state.db
    config = state.config
    library = library_stats(db)
    spotify = status_summary(db)
    spy = SpotifyAuth(config, db)
    spotify["logged_in"] = spy.is_logged_in()
    profile = db.get_preference(PROFILE_KEY) or {}
    spotify["profile_artists"] = len(profile.get("artists") or [])

    try:
        planner = SyncPlanner(config, db)
        candidates = DeviceLocator(config).candidates()
        volumes = [
            {**volume.as_dict(), "confirmed": planner.is_confirmed(volume)}
            for volume in candidates
        ]
        device_error = None
    except DeviceError as exc:
        volumes = []
        device_error = str(exc)

    payload = {
        "library": library,
        "spotify": spotify,
        "devices": volumes,
        "device_error": device_error,
        "tools": {
            "ffprobe": find_tool("ffprobe", config.audio.ffprobe_path),
            "ffmpeg": find_tool("ffmpeg", config.audio.ffmpeg_path),
        },
        "counts": {
            "candidates": db.scalar("SELECT COUNT(*) FROM discovery_candidates"),
            "awaiting_approval": db.scalar(
                "SELECT COUNT(*) FROM discovery_candidates WHERE status = 'awaiting_approval'"
            ),
            "acquisitions": db.scalar("SELECT COUNT(*) FROM acquisitions"),
            "sync_runs": db.scalar("SELECT COUNT(*) FROM sync_runs"),
        },
    }

    def render(data: dict[str, Any]) -> None:
        lib = data["library"]
        table(
            "Library",
            ["Metric", "Value"],
            [
                ["Files", lib["files"]],
                ["Verified", lib["validated"]],
                ["Verified FLAC", lib["verified_flac"]],
                ["Transcode suspects", lib["transcode_suspect"]],
                ["Albums", lib["albums"]],
                ["Artists", lib["artists"]],
                ["Size", f"{lib['size_gb']} GB"],
            ],
        )
        counts = data["spotify"].get("counts") or {}
        table(
            "Spotify",
            ["Metric", "Value"],
            [
                ["Connected", "yes" if data["spotify"].get("logged_in") else "no"],
                ["Client ID", "saved" if data["spotify"].get("client_id_configured") else "missing"],
                ["Saved tracks", counts.get("saved_tracks", 0)],
                ["Playlists", counts.get("playlists", 0)],
                ["Taste artists", data["spotify"].get("profile_artists", 0)],
                ["Last sync", data["spotify"].get("last_sync") or "never"],
            ],
        )
        if data["devices"]:
            table(
                "External volumes",
                ["Name", "Filesystem", "Size", "Confirmed", "Mount"],
                [
                    [
                        volume["volume_name"],
                        volume["filesystem"],
                        f"{volume['capacity_gb']} GB",
                        "yes" if volume.get("confirmed") else "no",
                        volume["mount_point"],
                    ]
                    for volume in data["devices"]
                ],
            )
        elif data["device_error"]:
            console.print(f"[yellow]Card:[/yellow] {data['device_error']}")
        tools = data["tools"]
        missing = [name for name, path in tools.items() if not path]
        if missing:
            console.print(f"[yellow]Missing tools:[/yellow] {', '.join(missing)} (brew install ffmpeg)")
        pipeline = data["counts"]
        table(
            "Pipeline",
            ["Metric", "Value"],
            [
                ["Discovery candidates", pipeline["candidates"]],
                ["Awaiting approval", pipeline["awaiting_approval"]],
                ["Acquisitions", pipeline["acquisitions"]],
                ["Sync runs", pipeline["sync_runs"]],
            ],
        )

    state.emit(payload, render)


@app.command()
def report(
    ctx: typer.Context,
    label: str = typer.Option(None, "--label", help="Report label suffix"),
) -> None:
    """Write Markdown and JSON library reports."""
    state = get_state(ctx)
    paths = write_report(state.config, state.db, label=label)
    state.emit({key: str(value) for key, value in paths.items()}, lambda p: [
        ok(f"{key}: {value}") for key, value in p.items()
    ])


# ------------------------------------------------------------------ spotify
@spotify_app.command("login")
def spotify_login(
    ctx: typer.Context,
    client_id: str = typer.Option(None, "--client-id", help="Spotify app Client ID"),
    no_browser: bool = typer.Option(False, "--no-browser", help="Print the URL instead of opening it"),
    timeout: int = typer.Option(300, "--timeout", help="Seconds to wait for the callback"),
) -> None:
    """Connect Spotify with OAuth PKCE (loopback redirect)."""
    state = get_state(ctx)
    auth = SpotifyAuth(state.config, state.db)
    if not client_id and not auth.client_id:
        console.print(Panel(SETUP_HELP, title="Spotify app required", border_style="yellow"))
        fail("No Client ID configured. Pass --client-id or set spotify.client_id in config.yaml")
    try:
        tokens = auth.login(open_browser=not no_browser, timeout=timeout)
    except SpotifyAuthError as exc:
        fail(str(exc))
    ok(f"Spotify connected. Scopes: {' '.join(tokens.get('scope', []))}")


@spotify_app.command("logout")
def spotify_logout(ctx: typer.Context) -> None:
    """Remove stored Spotify tokens."""
    SpotifyAuth(get_state(ctx).config, get_state(ctx).db).logout()
    ok("Spotify tokens removed")


@spotify_app.command("status")
def spotify_status(ctx: typer.Context) -> None:
    """Show Spotify connection and import status."""
    state = get_state(ctx)
    payload = status_summary(state.db)
    payload["logged_in"] = SpotifyAuth(state.config, state.db).is_logged_in()

    def render(data: dict[str, Any]) -> None:
        counts = data.get("counts") or {}
        table(
            "Spotify",
            ["Metric", "Value"],
            [
                ["Connected", "yes" if data["logged_in"] else "no"],
                ["Client ID", "saved" if data["client_id_configured"] else "missing"],
                ["Saved tracks", counts.get("saved_tracks", 0)],
                ["Playlists", counts.get("playlists", 0)],
                ["Artists", counts.get("artists", 0)],
                ["Albums", counts.get("albums", 0)],
                ["Tracks", counts.get("tracks", 0)],
                ["Last sync", data.get("last_sync") or "never"],
            ],
        )

    state.emit(payload, render)


@spotify_app.command("set-client-id")
def spotify_set_client_id(
    ctx: typer.Context,
    client_id: str = typer.Argument(..., help="Spotify app Client ID"),
) -> None:
    """Store the Spotify Client ID."""
    state = get_state(ctx)
    SpotifyAuth(state.config, state.db).set_client_id(client_id)
    ok("Client ID saved")


@spotify_app.command("sync")
def spotify_sync(
    ctx: typer.Context,
    full: bool = typer.Option(False, "--full", help="Ignore playlist snapshots"),
    seeds: bool = typer.Option(True, "--seeds/--no-seeds", help="Import seed playlists"),
) -> None:
    """Import saved music, playlists, artists, and listening history."""
    state = get_state(ctx)
    auth = SpotifyAuth(state.config, state.db)
    if not auth.is_logged_in():
        fail("Not connected. Run `musicctl spotify login --client-id <CLIENT_ID>`")
    client = SpotifyClient(state.config, state.db, auth=auth)
    try:
        with console.status("Importing from Spotify…"):
            stats = SpotifyImporter(state.config, state.db, client).run(include_seeds=seeds, full=full)
            profile = TasteProfiler(state.config, state.db).build()
    except SpotifyUnavailable as exc:
        fail(f"Spotify endpoint unavailable: {exc}")
    finally:
        client.close()
    payload = {"import": stats.as_dict(), "profile_artists": len(profile.get("artists") or [])}
    state.emit(payload, lambda p: table("Spotify import", ["Metric", "Value"], [
        [key, value] for key, value in sorted(p["import"].items()) if not isinstance(value, list)
    ] + [["profile_artists", p["profile_artists"]]]))


@spotify_app.command("profile")
def spotify_profile(
    ctx: typer.Context,
    limit: int = typer.Option(25, "--limit", help="Artists to include"),
    refresh: bool = typer.Option(False, "--refresh", help="Rebuild before printing"),
) -> None:
    """Show the inferred taste profile."""
    state = get_state(ctx)
    profiler = TasteProfiler(state.config, state.db)
    profile = profiler.load() if not refresh else None
    if profile is None:
        profile = profiler.build(limit=limit)
    if state.as_json:
        state.emit(profile)
        return
    console.print(profile_text(profile))


@spotify_app.command("changes")
def spotify_changes(ctx: typer.Context) -> None:
    """Show what changed on Spotify since the last import."""
    state = get_state(ctx)
    auth = SpotifyAuth(state.config, state.db)
    if not auth.is_logged_in():
        fail("Not connected. Run `musicctl spotify login`")
    client = SpotifyClient(state.config, state.db, auth=auth)
    try:
        changes = detect_changes(state.config, state.db, client)
    finally:
        client.close()
    state.emit(changes, lambda c: console.print(JSON.from_data(c, indent=2)))


@spotify_app.command("discover")
def spotify_discover(
    ctx: typer.Context,
    limit: int = typer.Option(15, "--limit", help="Artists to consider"),
) -> None:
    """Generate discovery candidates from Spotify data only."""
    _run_discovery(ctx, limit=limit, use_spotify=True, use_musicbrainz=False, use_internet_archive=False)


# ------------------------------------------------------------------ discovery
@app.command()
def discover(
    ctx: typer.Context,
    limit: int = typer.Option(12, "--limit", help="Artists to consider"),
    source: str = typer.Option("all", "--source", help="all, spotify, musicbrainz, or internet-archive"),
    no_spotify: bool = typer.Option(False, "--no-spotify"),
    no_musicbrainz: bool = typer.Option(False, "--no-musicbrainz"),
    no_archive: bool = typer.Option(False, "--no-internet-archive"),
) -> None:
    """Find additional releases from the taste profile and external catalogues."""
    use_spotify = not no_spotify and source in {"all", "spotify"}
    use_mb = not no_musicbrainz and source in {"all", "musicbrainz"}
    use_ia = not no_archive and source in {"all", "internet-archive"}
    _run_discovery(ctx, limit=limit, use_spotify=use_spotify, use_musicbrainz=use_mb, use_internet_archive=use_ia)


def _run_discovery(
    ctx: typer.Context,
    limit: int,
    use_spotify: bool,
    use_musicbrainz: bool,
    use_internet_archive: bool,
) -> None:
    state = get_state(ctx)
    spotify = None
    if use_spotify:
        auth = SpotifyAuth(state.config, state.db)
        if auth.is_logged_in():
            spotify = SpotifyClient(state.config, state.db, auth=auth)
        else:
            use_spotify = False
    engine = DiscoveryEngine(state.config, state.db, spotify=spotify)
    try:
        with console.status("Searching authorised sources…"):
            summary = engine.run(
                limit_artists=limit,
                use_spotify=use_spotify,
                use_musicbrainz=use_musicbrainz,
                use_internet_archive=use_internet_archive,
            )
    finally:
        engine.close()
        if spotify is not None:
            spotify.close()
    payload = summary.as_dict()
    state.emit(payload, lambda p: table("Discovery", ["Metric", "Value"], [
        ["Artists considered", p.get("artists")],
        ["Albums", p.get("albums")],
        ["Releases", p.get("releases")],
        ["Labels", p.get("labels")],
        ["Warnings", len(p.get("warnings") or [])],
    ]))
    rows = candidate_rows(state.db, limit=15)
    if rows and not state.as_json:
        table(
            "Top candidates",
            ["ID", "Artist", "Release", "Licence", "Status", "Reasons"],
            [
                [
                    row.get("id"),
                    (row.get("artist_name") or "")[:24],
                    (row.get("title") or "")[:34],
                    row.get("licence"),
                    row.get("status"),
                    ", ".join(reason.get("code", "") for reason in (row.get("reasons") or []))[:40],
                ]
                for row in rows
            ],
        )


@app.command()
def sources(ctx: typer.Context) -> None:
    """List configured discovery/acquisition sources."""
    state = get_state(ctx)
    rows = [dict(row) for row in state.db.query("SELECT * FROM sources ORDER BY name")]
    state.emit(rows, lambda data: table("Sources", ["Name", "Kind", "Base URL", "Enabled"], [
        [row["name"], row["kind"], row.get("base_url"), "yes" if row["enabled"] else "no"] for row in data
    ]))


@app.command()
def candidates(
    ctx: typer.Context,
    status: str = typer.Option(None, "--status", help="Filter by candidate status"),
    limit: int = typer.Option(50, "--limit"),
) -> None:
    """List discovery candidates."""
    state = get_state(ctx)
    rows = candidate_rows(state.db, status=status, limit=limit)
    state.emit(rows, lambda data: table("Candidates", ["ID", "Artist", "Release", "Score", "Licence", "Status"], [
        [row["id"], (row.get("artist_name") or "")[:24], (row.get("title") or "")[:36],
         row.get("score"), row.get("licence"), row.get("status")] for row in data
    ]))


# ------------------------------------------------------------------ queue / downloads
@queue_app.command("approve")
def queue_approve(
    ctx: typer.Context,
    ids: list[int] = typer.Argument(..., help="Candidate IDs"),
    notes: str = typer.Option(None, "--notes"),
) -> None:
    """Approve candidates for acquisition."""
    state = get_state(ctx)
    manager = DownloadManager(state.config, state.db)
    for candidate_id in ids:
        try:
            manager.approve(candidate_id, notes=notes)
            ok(f"Candidate {candidate_id} approved")
        except KeyError:
            fail(f"Candidate {candidate_id} not found / no authorised release")
        except AcquisitionBlocked as exc:
            fail(f"Candidate {candidate_id} blocked: {exc}")


@queue_app.command("reject")
def queue_reject(
    ctx: typer.Context,
    ids: list[int] = typer.Argument(..., help="Candidate IDs"),
    notes: str = typer.Option(None, "--notes"),
) -> None:
    """Reject candidates."""
    state = get_state(ctx)
    manager = DownloadManager(state.config, state.db)
    for candidate_id in ids:
        manager.reject(candidate_id, notes=notes)
        ok(f"Candidate {candidate_id} rejected")


@queue_app.command("show")
def queue_show(ctx: typer.Context) -> None:
    """Show candidates awaiting approval."""
    state = get_state(ctx)
    rows = candidate_rows(state.db, status="awaiting_approval", limit=100)
    state.emit(rows, lambda data: table("Awaiting approval", ["ID", "Artist", "Release", "Licence", "Score"], [
        [row["id"], (row.get("artist_name") or "")[:24], (row.get("title") or "")[:36],
         row.get("licence"), row.get("score")] for row in data
    ]))


@queue_app.command("run")
def queue_run(
    ctx: typer.Context,
    limit: int = typer.Option(None, "--limit", help="Maximum acquisitions to queue"),
    paused: bool = typer.Option(False, "--paused", help="Add paused for review in qBittorrent"),
    process: bool = typer.Option(False, "--process", help="Validate and organise completed downloads"),
) -> None:
    """Send approved acquisitions to qBittorrent."""
    state = get_state(ctx)
    qbit = QBittorrentClient(state.config)
    manager = DownloadManager(state.config, state.db, qbit=qbit)
    try:
        if not state.config.downloads.enabled:
            fail("Downloads are disabled (set downloads.enabled: true in config.yaml)")
        if paused and not state.config.downloads.approval_required:
            console.print("[yellow]Paused mode is safest; approval_required is false.[/yellow]")
        with console.status("Queuing approved releases…"):
            result = manager.queue(limit=limit, paused=paused or state.config.downloads.approval_required)
        payload = result.as_dict()
        state.emit(payload, lambda p: table("Queue", ["Metric", "Value"], [
            ["Queued", len(p["queued"])], ["Skipped", len(p["skipped"])], ["Errors", len(p["errors"])]
        ]))
        for item in result.queued:
            ok(f"Queued {item.get('release')} ({(item.get('hash') or '')[:12]}…)")
        for item in result.skipped:
            console.print(
                f"[dim]Skipped acquisition {item.get('acquisition_id')}: {item.get('reason')}[/dim]"
            )
        for message in result.errors:
            console.print(f"[red]✗[/red] {message}")
        if process:
            done = manager.process_downloaded()
            state.emit(done, lambda p: table("Processed", ["Metric", "Value"], [
                [key, value] for key, value in p.items() if not isinstance(value, (list, dict))
            ]))
    except (QBittorrentError, AcquisitionBlocked) as exc:
        fail(str(exc))
    finally:
        qbit.close()


@downloads_app.command("status")
def downloads_status(ctx: typer.Context) -> None:
    """Show qBittorrent availability and acquisition progress."""
    state = get_state(ctx)
    qbit = QBittorrentClient(state.config)
    manager = DownloadManager(state.config, state.db, qbit=qbit)
    try:
        payload = manager.status()
    finally:
        qbit.close()
    state.emit(payload, lambda p: (
        table("Downloads", ["Metric", "Value"], [
            ["Enabled", p["enabled"]],
            ["qBittorrent", p["qbit_version"] or ("reachable" if p["qbit_available"] else "unavailable")],
            ["Disk", p["disk"]],
        ]),
        table("Jobs", ["ID", "Release", "Status", "Licence", "Progress"], [
            [job.get("id"), (job.get("release_title") or "")[:34], job.get("status"),
             job.get("licence"), job.get("progress")] for job in p.get("jobs") or []
        ]),
    ))


@app.command("download")
def download_cmd(
    ctx: typer.Context,
    track: int = typer.Option(None, "--track", "-t", help="Track ID to download"),
    playlist: int = typer.Option(None, "--playlist", "-p", help="Playlist ID to download"),
    saved: bool = typer.Option(False, "--saved", "-s", help="Download top saved/liked tracks"),
    limit: int = typer.Option(20, "--limit", "-l", help="Limit number of tracks for playlist/saved"),
) -> None:
    """Download audio tracks, tag with Vorbis comments, validate, and organise for Echo Mini."""
    from fiiolib.downloads.downloader import (
        download_playlist,
        download_saved_tracks,
        download_track,
    )

    state = get_state(ctx)
    if track:
        console.print(f"[cyan]Downloading track #{track}…[/cyan]")
        res = download_track(state.config, state.db, track)
        if res.get("ok"):
            ok(f"Downloaded '{res.get('title')}' by {res.get('artist')} -> {res.get('path')}")
        else:
            fail(f"Download failed: {res.get('error')}")
    elif playlist:
        console.print(f"[cyan]Downloading playlist #{playlist} (up to {limit} tracks)…[/cyan]")
        res = download_playlist(state.config, state.db, playlist, max_tracks=limit)
        ok(
            f"Playlist download complete: {res['downloaded']} downloaded, {res['skipped']} skipped, {len(res['errors'])} errors"
        )
    elif saved:
        console.print(f"[cyan]Downloading top {limit} liked songs…[/cyan]")
        res = download_saved_tracks(state.config, state.db, limit=limit)
        ok(
            f"Liked songs complete: {res['downloaded']} downloaded, {res['skipped']} skipped, {len(res['errors'])} errors"
        )
    else:
        fail("Specify --track <id>, --playlist <id>, or --saved")


# ------------------------------------------------------------------ library
@app.command()
def scan(
    ctx: typer.Context,
    path: list[Path] = typer.Option(None, "--path", "-p", help="Restrict to these directories"),
    fast: bool = typer.Option(False, "--fast", help="Skip hashing"),
) -> None:
    """Index audio files in the library."""
    state = get_state(ctx)
    scanner = LibraryScanner(state.config, state.db)
    with console.status("Scanning…"):
        summary = scanner.scan(paths=path, compute_hashes=not fast)
    payload = summary.as_dict()
    state.emit(payload, lambda p: table("Scan", ["Metric", "Value"], [
        [key, value] for key, value in p.items() if not isinstance(value, (list, dict))
    ]))


@app.command()
def validate(
    ctx: typer.Context,
    path: list[Path] = typer.Option(None, "--path", "-p", help="Files or directories"),
    limit: int = typer.Option(0, "--limit", help="Maximum files (0 = all)"),
    json_output_local: bool = typer.Option(False, "--json", help="JSON output"),
) -> None:
    """Probe and validate audio against Echo Mini compatibility rules."""
    state = get_state(ctx)
    roots = path or [state.config.verified_dir, state.config.downloads_dir, state.config.music_dir]
    extensions = {value.lower() for value in state.config.library.audio_extensions}
    files: list[Path] = []
    for root in roots:
        root = Path(root).expanduser()
        if root.is_file():
            files.append(root)
        elif root.exists():
            files.extend(p for p in sorted(root.rglob("*")) if p.is_file() and p.suffix.lower() in extensions)
    if limit:
        files = files[:limit]
    if not files:
        fail("No audio files found to validate")
    results = []
    for file_path in files:
        result = validate_audio(
            file_path,
            ffprobe_path=state.config.audio.ffprobe_path,
            require_device_compat=True,
        )
        results.append(result)
        _store_validation(state.db, result)
    payload = {
        "files": len(results),
        "ok": sum(1 for result in results if result.ok),
        "failed": sum(1 for result in results if not result.ok),
        "transcode_suspect": sum(1 for result in results if result.transcode_suspect),
        "problems": [
            {"path": result.path, "errors": result.errors}
            for result in results
            if not result.ok
        ][:100],
    }
    if state.as_json or json_output_local:
        console.print(JSON.from_data(payload, indent=2))
        return
    table("Validation", ["Metric", "Value"], [
        ["Files", payload["files"]], ["Compatible", payload["ok"]],
        ["Failed", payload["failed"]], ["Transcode suspects", payload["transcode_suspect"]],
    ])
    for problem in payload["problems"][:15]:
        console.print(f"[red]✗[/red] {problem['path']}: {'; '.join(problem['errors'])}")


def _store_validation(db: Database, result: Any) -> None:
    probe = result.probe
    notes = "; ".join([*result.findings, *result.warnings, *result.errors]) or None
    values = (
        probe.size_bytes if probe else Path(result.path).stat().st_size,
        Path(result.path).stat().st_mtime,
        result.sha256,
        probe.format_name if probe else None,
        probe.codec if probe else None,
        probe.bit_depth if probe else None,
        probe.sample_rate if probe else None,
        probe.channels if probe else None,
        probe.bitrate if probe else None,
        probe.duration_ms if probe else None,
        1 if result.ok else 0,
        notes,
        1 if result.transcode_suspect else 0,
        utcnow(),
        result.path,
    )
    row = db.one("SELECT id FROM files WHERE path = ?", (result.path,))
    if row:
        db.execute(
            "UPDATE files SET size_bytes = ?, mtime = ?, sha256 = ?, format = ?, codec = ?, bit_depth = ?, "
            "sample_rate = ?, channels = ?, bitrate = ?, duration_ms = ?, validated = ?, validation_notes = ?, "
            "transcode_suspect = ?, updated_at = ? WHERE path = ?",
            values,
        )
    else:
        db.execute(
            "INSERT INTO files(path, size_bytes, mtime, sha256, format, codec, bit_depth, sample_rate, "
            "channels, bitrate, duration_ms, validated, validation_notes, transcode_suspect, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (result.path, *values[:-1]),
        )


@app.command()
def organise(
    ctx: typer.Context,
    path: list[Path] = typer.Option(None, "--path", "-p"),
    copy: bool = typer.Option(False, "--copy", help="Copy instead of move"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview only"),
) -> None:
    """Move verified files into the genre/artist/album structure."""
    state = get_state(ctx)
    organiser = Organiser(state.config, state.db)
    paths = path or [state.config.verified_dir]
    results = []
    for target in paths:
        target = Path(target).expanduser()
        if target.is_file():
            track = state.db.one("SELECT track_id FROM files WHERE path = ?", (str(target),))
            results.append(organiser.organise(target, track_id=track["track_id"] if track else None, copy=copy))
        elif target.exists():
            results.extend(organiser.organise_directory(target, copy=copy, dry_run=dry_run))
    payload = [result.as_dict() for result in results]
    state.emit(payload, lambda rows: table("Organise", ["Source", "Destination", "Action"], [
        [Path(row["source"]).name, row.get("destination"), row["action"]] for row in rows
    ]))


@app.command()
def deduplicate(ctx: typer.Context) -> None:
    """Report duplicate files; never deletes anything."""
    state = get_state(ctx)
    summary = Deduplicator(state.config, state.db).summary()
    state.emit(summary, lambda data: table("Duplicates", ["Metric", "Value"], [
        ["Groups", data["groups"]],
        ["Duplicate files", data["duplicate_files"]],
        ["Reclaimable", f"{data['reclaimable_gb']} GB"],
        ["Note", data["note"]],
    ]))


@metadata_app.command("refresh")
def metadata_refresh(ctx: typer.Context) -> None:
    """Scan for untagged files and match them to known tracks."""
    state = get_state(ctx)
    result = Scheduler(state.config, state.db).run_job("metadata_refresh")
    if result.status != "completed":
        fail(result.error or "Metadata refresh failed")
    state.emit(result.detail, lambda data: table("Metadata", ["Metric", "Value"], [
        ["Scanned", (data.get("scanned") or {}).get("scanned")],
        ["Matched", data.get("matched")],
    ]))


# ------------------------------------------------------------------ devices / sync
@device_app.command("list")
def device_list(ctx: typer.Context) -> None:
    """List mounted external volumes."""
    _show_devices(ctx)


@device_app.command("inspect")
def device_inspect(
    ctx: typer.Context,
    device: str = typer.Option(None, "--device", help="Volume name, UUID, or device node"),
) -> None:
    """Inspect an external card before confirming it."""
    state = get_state(ctx)
    locator = DeviceLocator(state.config)
    try:
        volumes = locator.candidates()
    except DeviceError as exc:
        fail(str(exc))
        return
    if device:
        volume = locator.current(device)
        volumes = [volume]
    if not volumes:
        fail("No external SD card volumes are mounted")
    planner = SyncPlanner(state.config, state.db)
    rows = []
    for volume in volumes:
        db_id = planner.register(volume)
        planner.db.execute("UPDATE devices SET is_echo_mini = ? WHERE id = ?",
                           (1 if volume.is_echo_candidate else 0, db_id))
        rows.append([
            volume.volume_name, volume.filesystem, f"{volume.capacity_gb} GB",
            volume.mount_point, device_identifier(volume)[:24],
            "yes" if planner.is_confirmed(volume) else "no",
        ])
    if state.as_json:
        state.emit([volume.as_dict() for volume in volumes])
        return
    table("Volumes", ["Name", "FS", "Size", "Mount", "Identifier", "Confirmed"], rows)
    for volume in volumes:
        console.print(f"[dim]{volume.volume_name}:[/dim] {'; '.join(volume.reasons)}")
    console.print(
        Panel(
            "Confirm only the card you intend to write to. The expected device is "
            f"{state.config.device.expected_player} with a {state.config.device.expected_card_capacity_gb} GB card.\n"
            f"After checking the mount point and capacity, run:\n"
            f"  musicctl device confirm --device {volumes[0].volume_name!r}",
            title="Before you confirm",
            border_style="yellow",
        )
    )


@device_app.command("confirm")
def device_confirm(
    ctx: typer.Context,
    device: str = typer.Option(None, "--device", help="Volume name, UUID, or device node"),
    yes: bool = typer.Option(False, "--yes", help="Skip the interactive prompt"),
) -> None:
    """Mark a card as the trusted sync target."""
    state = get_state(ctx)
    planner = SyncPlanner(state.config, state.db)
    volume = DeviceLocator(state.config).current(device)
    console.print(
        f"Volume: [bold]{volume.volume_name}[/bold]  FS: {volume.filesystem}  "
        f"Size: {volume.capacity_gb} GB  Mount: {volume.mount_point}"
    )
    if not yes and not typer.confirm(
        "Is this the microSD card for the SNOWSKY Echo Mini?", default=False
    ):
        fail("Confirmation cancelled")
    planner.confirm(volume)
    ok(f"Confirmed {volume.volume_name} ({device_identifier(volume)})")


def _show_devices(ctx: typer.Context) -> None:
    state = get_state(ctx)
    try:
        volumes = DeviceLocator(state.config).candidates()
    except DeviceError as exc:
        fail(str(exc))
        return
    planner = SyncPlanner(state.config, state.db)
    rows = [[
        volume.volume_name, volume.filesystem, f"{volume.capacity_gb} GB",
        "yes" if volume.is_echo_candidate else "no",
        "yes" if planner.is_confirmed(volume) else "no",
        volume.mount_point,
    ] for volume in volumes]
    state.emit([volume.as_dict() for volume in volumes],
               lambda _: table("External volumes", ["Name", "FS", "Size", "Candidate", "Confirmed", "Mount"], rows))


@app.command()
def sync(
    ctx: typer.Context,
    device: str = typer.Option(None, "--device", help="Volume name, UUID, or device node"),
    profile: str = typer.Option("complete", "--profile", help="complete, favourites, discovery, daily, genre, offline"),
    genre: list[str] = typer.Option(None, "--genre", "-g", help="Restrict to genre(s)"),
    artist_id: list[int] = typer.Option(None, "--artist-id", help="Restrict to artist IDs"),
    album_id: list[int] = typer.Option(None, "--album-id", help="Restrict to album IDs"),
    fast: bool = typer.Option(False, "--fast", help="Skip checksum reads on the card"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview only (default unless --yes)"),
    yes: bool = typer.Option(False, "--yes", help="Actually copy (otherwise dry-run)"),
) -> None:
    """Plan a checksummed sync to the confirmed microSD card."""
    state = get_state(ctx)
    if dry_run:
        yes = False
    planner = SyncPlanner(state.config, state.db)
    try:
        plan = planner.plan(
            device_identifier_hint=device,
            profile=profile,
            genres=genre,
            artist_ids=artist_id,
            album_ids=album_id,
            fast=fast,
        )
    except DeviceError as exc:
        fail(str(exc))
        return
    payload = plan.as_dict()
    if state.as_json:
        state.emit(payload)
    else:
        table("Sync plan", ["Metric", "Value"], [
            ["Device", f"{plan.device.volume_name} ({plan.device.capacity_gb} GB, {plan.device.filesystem})"],
            ["Profile", plan.profile],
            ["Add", plan.counts.get("add", 0)],
            ["Update", plan.counts.get("update", 0)],
            ["Skip", plan.counts.get("skip", 0)],
            ["No space", plan.counts.get("no_space", 0)],
            ["Planned", f"{plan.planned_bytes / 1024**3:.2f} GB"],
            ["Free", f"{plan.free_bytes / 1024**3:.2f} GB"],
            ["Reserve", f"{plan.reserve_bytes / 1024**3:.2f} GB"],
            ["Fits", "yes" if plan.fits else "no"],
            ["Unmanaged files on card", len(plan.unmanaged)],
        ])
        for warning in plan.warnings:
            console.print(f"[yellow]![/yellow] {warning}")
        if plan.unmanaged:
            console.print(
                f"[yellow]{len(plan.unmanaged)} audio files are on the card but not in this plan; "
                "they will be left untouched.[/yellow]"
            )
        if plan.items:
            table("Files", ["Action", "Size", "Destination"], [
                [item.action, f"{item.size / 1024**2:.1f} MB", item.relative]
                for item in plan.items[:40]
            ])
            if len(plan.items) > 40:
                console.print(f"[dim]… and {len(plan.items) - 40} more[/dim]")
    if not plan.fits:
        fail("Plan does not fit in the reserved free space. Narrow the selection or use --profile offline.")
    if plan.counts.get("no_space"):
        console.print("[yellow]Some files were marked no_space and will be skipped.[/yellow]")
    if not yes:
        console.print("[bold]Dry run.[/bold] Re-run with [bold]--yes[/bold] to copy these files.")
        return
    if not typer.confirm(
        f"Write {plan.counts.get('add', 0) + plan.counts.get('update', 0)} files to "
        f"{plan.device.mount_point}?",
        default=False,
    ):
        fail("Cancelled")
    with console.status("Copying and verifying…"):
        result = planner.execute(plan, dry_run=False, verify=True)
    state.emit(result, lambda data: table("Sync result", ["Metric", "Value"], [
        ["Added", data["added"]], ["Updated", data["updated"]], ["Skipped", data["skipped"]],
        ["Verified", data["verified"]], ["Errors", len(data["errors"])],
    ]))
    for error in result["errors"][:10]:
        console.print(f"[red]✗[/red] {error}")


# ------------------------------------------------------------------ maintenance
@app.command()
def backup(ctx: typer.Context) -> None:
    """Create a consistent SQLite backup."""
    state = get_state(ctx)
    destination = state.db.backup(
        state.config.backups_dir / f"library-{utcnow().replace(':', '')}.sqlite3"
    )
    ok(f"Backup written to {destination}")


@app.command("integrity-check")
def integrity_check(
    ctx: typer.Context,
    deep: bool = typer.Option(False, "--deep", help="Open every file and probe it"),
) -> None:
    """Validate database integrity, missing paths, and checksums."""
    state = get_state(ctx)
    integrity = state.db.integrity_check()
    problems = LibraryScanner(state.config, state.db).integrity_check(deep=deep)
    payload = {"database": integrity, "problems": problems, "problem_count": len(problems)}
    state.emit(payload, lambda data: (
        table("Database", ["Metric", "Value"], [["Integrity", "ok" if data["database"]["ok"] else "failed"]]),
        table("Problems", ["Path", "Issue"], [
            [problem.get("path"), problem.get("issue")] for problem in data["problems"][:25]
        ]),
    ))
    if not integrity["ok"]:
        fail("Database integrity check failed")


@app.command()
def logs(
    ctx: typer.Context,
    lines: int = typer.Option(50, "--lines", "-n"),
) -> None:
    """Tail the structured log file."""
    state = get_state(ctx)
    path = state.config.logs_dir / "fiiolib.jsonl"
    if not path.exists():
        fail(f"No log file at {path}")
        return
    content = path.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:]
    for line in content:
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            console.print(line)
            continue
        console.print(
            f"[dim]{record.get('time', '')}[/dim] [bold]{record.get('level', '')}[/bold] "
            f"{record.get('logger', '')}: {record.get('message', '')}"
        )


@automation_app.command("status")
def automation_status(ctx: typer.Context) -> None:
    """Show schedules and last run times."""
    state = get_state(ctx)
    scheduler = Scheduler(state.config, state.db)
    rows = []
    for name in JOB_NAMES:
        last = scheduler.last_run(name)
        rows.append([name, scheduler.schedule_for(name), last.isoformat() if last else "never",
                     "yes" if scheduler.is_due(name) else "no"])
    state.emit([{"name": row[0], "schedule": row[1], "last_run": row[2], "due": row[3] == "yes"} for row in rows],
               lambda _: table("Automation", ["Job", "Schedule", "Last run", "Due"], rows))


@automation_app.command("run")
def automation_run(
    ctx: typer.Context,
    job: str = typer.Option(None, "--job", help="Run one job instead of all due jobs"),
) -> None:
    """Run due automation jobs (used by launchd)."""
    state = get_state(ctx)
    scheduler = Scheduler(state.config, state.db)
    try:
        outcomes = [scheduler.run_job(job)] if job else scheduler.run_due()
    except SchedulerError as exc:
        fail(str(exc))
        return
    if not outcomes:
        console.print("No jobs due.")
        return
    rows = [[outcome.name, outcome.status, outcome.error or ""] for outcome in outcomes]
    state.emit([outcome.as_dict() for outcome in outcomes],
               lambda _: table("Automation run", ["Job", "Status", "Error"], rows))
    if any(outcome.status == "failed" for outcome in outcomes):
        fail("One or more automation jobs failed")


@automation_app.command("install")
def automation_install(
    ctx: typer.Context,
    interval: int = typer.Option(30, "--interval", help="Minutes between checks"),
) -> None:
    """Install a launchd agent that runs due jobs."""
    state = get_state(ctx)
    try:
        path = Scheduler(state.config, state.db).install_launchd(interval_minutes=interval)
    except SchedulerError as exc:
        fail(str(exc))
        return
    ok(f"Installed {path}")


@automation_app.command("uninstall")
def automation_uninstall(ctx: typer.Context) -> None:
    """Remove the launchd agent."""
    removed = Scheduler(get_state(ctx).config, get_state(ctx).db).uninstall_launchd()
    ok("Removed launchd agent" if removed else "No launchd agent was installed")


@app.command()
def dashboard(
    ctx: typer.Context,
    host: str = typer.Option(None, "--host"),
    port: int = typer.Option(None, "--port"),
) -> None:
    """Serve the local read-only dashboard."""
    state = get_state(ctx)
    import uvicorn

    from fiiolib.dashboard import create_app

    bind_host = host or state.config.dashboard.host
    bind_port = port or state.config.dashboard.port
    if bind_host not in {"127.0.0.1", "localhost", "::1"}:
        fail("The dashboard only binds to localhost")
    console.print(f"Dashboard: [cyan]http://{bind_host}:{bind_port}[/cyan]")
    uvicorn.run(create_app(state.config, state.db), host=bind_host, port=bind_port, log_level="warning")


def run() -> None:
    try:
        app()
    except (DeviceError, SpotifyAuthError, SchedulerError) as exc:
        err_console.print(f"[red]✗[/red] {exc}")
        sys.exit(1)


if __name__ == "__main__":
    run()
