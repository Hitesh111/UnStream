# FiiO Library Manager

A local, macOS-first music library manager for a **FiiO SNOWSKY Echo Mini**:

- Reads your Spotify library, playlists, follows, and listening history to build a
  **taste profile**.
- Finds **additional releases** from MusicBrainz, Internet Archive, and the taste
  profile itself, with a reason code for every candidate.
- Only auto-approves releases whose **licence/authorisation is explicit**
  (public domain, Creative Commons, artist/label-authorised, or official free
  download). Everything else waits for manual review.
- Verifies audio against the Echo Mini's actual constraints (format, depth,
  sample rate, FLAC block size, stereo, clipping/transcode heuristics).
- Tags files, embeds artwork, and organises them into
  `Music/<Category>/<Subgenre>/<Artist>/<Album>/`.
- Syncs to the microSD card **incrementally and checksum-verified**, with a
  confirmed device identity, a capacity reserve, and no deletions.

Everything is local: SQLite database, JSON-lines logs, Markdown/JSON reports,
a loopback-only dashboard, and an optional launchd agent.

## Safety model

- **No unverified rights, no automatic downloads.** Candidate approval is gated
  on explicit licence data. Unknown rights are always surfaced for review.
- **No deleting music, ever.** Duplicate detection only writes a report. Sync
  never removes files from the card; unmanaged card files are listed and left
  alone.
- **No silent card writes.** Device confirmation is required once, and `sync`
  is a dry-run unless you pass `--yes`.
- **No executing downloads.** Audio files are read as data and probed; scripts
  and archives are ignored.
- Path traversal is blocked; copied files are written to a temporary name,
  re-hashed, then atomically renamed.

## Requirements

- macOS (tested on 15.x, Apple silicon), Python 3.12+
- FFmpeg (`ffprobe` and `ffmpeg`) for authoritative probing, artwork
  conversion, and transcoding suggestions (a mutagen fallback reads container
  properties for WAV/OGG/MP3/M4A/DSF when ffprobe is absent):
  ```sh
  brew install ffmpeg
  ```
- qBittorrent with the Web UI enabled, only if you want automatic downloads.
- A Spotify account (the app owner needs Premium) and a Spotify developer app.
- A microSD card formatted **ExFAT/FAT32** (the Echo Mini does not support NTFS)
  in a card reader.

Pure-FLAC probing works without FFmpeg via the FLAC STREAMINFO header, and a
mutagen fallback covers container-level properties for WAV, OGG, MP3, M4A, and
DSF. FLAC STREAMINFO plus FFmpeg remains the authoritative path (block-size,
Vorbis-comment, and artwork checks).

## Install

```sh
cd fiio-library-manager
python3.12 -m venv .venv
.venv/bin/pip install -e ".[dev]"
# optional test fixtures (FLAC generation)
.venv/bin/pip install -e ".[fixtures]"
```

`musicctl` is now available at `.venv/bin/musicctl`.

## First run

```sh
.venv/bin/musicctl init
.venv/bin/musicctl status
```

`init` writes `~/.config/fiio-library/config.yaml` (unless `--config` or
`FIOLIB_CONFIG` is set), creates the library tree at `~/FiiO-Library`, and
migrates the SQLite database.

### Spotify setup

1. Create an app at <https://developer.spotify.com/dashboard>.
2. Add the exact redirect URI printed by `musicctl init`
   (default `http://127.0.0.1:8888/callback`).
3. Connect:

   ```sh
   musicctl spotify login --client-id <CLIENT_ID>
   musicctl spotify sync
   musicctl spotify profile
   ```

PKCE is used: the client secret is never stored. Tokens live in the macOS
Keychain (`security`), with a `0600` file fallback.

Development-mode Spotify apps are limited (1 client ID, 5 users, no batch
endpoints). The client handles that by fetching individually with polite rate
limits; see [LIMITATIONS.md](LIMITATIONS.md).

### Discovery and approval

```sh
musicctl discover                  # Spotify + MusicBrainz + Internet Archive
musicctl discover --source musicbrainz
musicctl candidates --status awaiting_approval
musicctl queue approve 12 14
musicctl queue reject 9 --notes "unknown rights"
```

Candidates carry reason codes: `repeated_artist`, `playlist_overlap`,
`saved_artist`, `not_owned`, `earlier_release`, `era_match`, `label_match`,
`musicbrainz_release`, `archive_creator`, `licence`, `manual_review`, and more.

### Downloads (optional)

Enable qBittorrent in `config.yaml`:

```yaml
downloads:
  enabled: true
  approval_required: true
  qbittorrent_url: http://127.0.0.1:8080
  qbittorrent_username: admin
```

Store the Web UI password once (Keychain):

```sh
musicctl queue run --limit 3 --paused
```

Approved releases are added **paused** by default so you can inspect them. Once
completed:

```sh
musicctl downloads status
musicctl queue run --process      # validate -> match -> tag -> organise
```

Unmatched files are copied to `verified/unmatched-<id>/` and never force-fitted
to the wrong track.

### Minimal torrent fetch

If you do not want the Spotify/library pipeline and only need a magnet link or
`.torrent` saved straight to a folder (for example the SD card):

```sh
.venv/bin/pip install -e ".[torrent]"
torrent-fetch "magnet:?xt=urn:btih:..." -d /Volumes/sandisk64
torrent-fetch ./album.torrent -d ~/Downloads
```

`torrent-fetch` writes directly to the `-d` folder, keeps partial data on
interruption (rerun to resume after a piece recheck), and stops after the
download completes. Use it only for content you are authorised to download.

### Library maintenance

```sh
musicctl scan
musicctl validate
musicctl organise --dry-run
musicctl deduplicate
musicctl metadata refresh
musicctl report
musicctl integrity-check --deep
musicctl backup
```

### SD card and Echo Mini

1. Insert the card into the Mac.
2. `musicctl device inspect`
3. Verify the volume name, filesystem, capacity, and mount point, then:
   ```sh
   musicctl device confirm --device sandisk64
   ```
4. Plan and sync:
   ```sh
   musicctl sync --dry-run
   musicctl sync --profile favourites --yes
   ```

Profiles: `complete`, `favourites`, `discovery`, `daily`, `genre`, `offline`.
`offline` greedily fills the free space (minus `device.reserve_free_gb`) by
taste score.

Sync copies into `Music/`, preserving the organised structure. Existing files
are compared by size then SHA-256; identical files are skipped, differing files
are atomically replaced after verification. Card files outside the plan are
reported as **unmanaged** and left untouched.

### Automation and dashboard

```sh
musicctl automation status
musicctl automation run            # run due jobs once
musicctl automation install        # launchd agent, every 30 minutes
musicctl automation uninstall
musicctl dashboard                 # http://127.0.0.1:8765
```

The dashboard is read-only and binds to localhost only. It shows library stats,
Spotify state, mounted cards, pipeline counts, sync runs, acquisitions, and
automation schedules.

## Command reference

| Command | Purpose |
| --- | --- |
| `init` | Config, directories, database |
| `status` | Library, Spotify, card, pipeline summary |
| `report` | Markdown + JSON reports |
| `spotify login/logout/status/set-client-id/sync/profile/changes/discover` | Spotify OAuth and import |
| `discover` | Candidate generation (source-selectable) |
| `sources` | Configured discovery sources |
| `candidates` | List candidates |
| `queue approve/reject/show/run` | Rights-gated acquisition queue |
| `downloads status` | qBittorrent and job progress |
| `scan`, `validate`, `organise`, `deduplicate` | Library workflows |
| `metadata refresh` | Match untagged files to known tracks |
| `device list/inspect/confirm` | Card identity |
| `sync` | Dry-run-first, checksummed card sync |
| `backup`, `integrity-check`, `logs` | Maintenance |
| `automation status/run/install/uninstall` | Scheduled jobs |
| `dashboard` | Local web UI |

Global flags: `--config PATH`, `--json`, `--verbose`, `--version`.

## Testing

```sh
.venv/bin/pip install -e ".[dev,fixtures]"
.venv/bin/pytest
.venv/bin/ruff check src tests
```

Tests generate real FLAC fixtures with `soundfile` and cover probing, device
compatibility, classification, licensing, organisation, dedupe reporting, sync
planning/execution, and the full authorised-acquisition-to-card pipeline using
a fake mount point. No network, no real card writes.

## Project layout

```
src/fiiolib/
  config.py            typed configuration
  db/                  schema, migrations, repositories
  spotify/             PKCE auth, client, importer, taste profile
  discovery/           MusicBrainz, Internet Archive, licence policy, candidates
  audio/               probing, validation, device profile
  metadata/            classification, tags, artwork, matching
  library/             organiser, scanner, dedupe, reports
  downloads/           qBittorrent client, approval/queue/post-process
  device/              macOS volume detection
  sync/                selection profiles, planner, checksummed copier
  automation/          scheduler and launchd
  dashboard/           local FastAPI UI
  cli/                 Typer commands
```

See [ARCHITECTURE.md](ARCHITECTURE.md) for data flow and
[LIMITATIONS.md](LIMITATIONS.md) for device/API caveats.
