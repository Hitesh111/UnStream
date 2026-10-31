# Architecture

## Data flow

```
                      ┌──────────────────────────────┐
 Spotify Web API ───► │ spotify/auth  (PKCE, Keychain)│
                      │ spotify/client (retry, paging)│
                      │ spotify/sync   (importer)     │
                      └──────────────┬───────────────┘
                                     │ artists, albums, tracks,
                                     │ playlists, saved, recent, top
                                     ▼
                         ┌───────────────────────┐
                         │ SQLite (db/schema)    │
                         │ artists/albums/tracks │
                         │ playlists, spotify_*  │
                         └───────┬───────┬───────┘
                                 │       │
             spotify/taste ──────┘       └────── discovery/engine
        (weighted profile, reasons)              │
                                                 │ MusicBrainz
                                                 │ Internet Archive
                                                 │ + licence policy
                                                 ▼
                                       discovery_candidates
                                       sources + releases
                                                 │
                                    queue approve (rights gate)
                                                 ▼
                                       acquisitions ──► torrent_jobs
                                                 │      (qBittorrent)
                                                 ▼
                            audio/validate ─► metadata/matcher
                                                 │
                                            metadata/tagger
                                                 │
                                            library/organiser
                                                 ▼
                                          Music/<Category>/...
                                                 │
                       sync/selection ─► sync/planner ─► checksummed copy
                                                 ▼
                                          SD card /Music
```

## Core decisions

- **SQLite with WAL and explicit transactions.** `Database` uses
  `isolation_level=None`, `BEGIN IMMEDIATE` transactions for writes, foreign
  keys, and a busy timeout. Schema versioning lives in `db/schema.sql` with a
  `schema_version` setting.
- **Repository layer.** `db/repo.py` provides idempotent `get_or_create_*`
  helpers and upserts. Spotify IDs and MusicBrainz IDs are both carried so
  imports are repeatable without duplicates.
- **Spotify importer is incremental.** Playlist items are only fetched for
  playlists the user owns or collaborates on (Feb 2026 API rule). Snapshot IDs
  are compared before re-import; a full import can be requested. First import
  deliberately imports items even without a prior snapshot.
- **Taste profile is transparent.** Source weights (saved track 3.0, saved album
  2.0, personal playlist 1.5, collaborative 1.2, followed artist 0.5, seed 0.3)
  are decayed by recency and aggregated into artist weights, genres, labels,
  eras, instrumental ratio, and playlist overlap. The profile is stored in
  `user_preferences` and shown by `musicctl spotify profile`.
- **Discovery is explainable.** Every candidate has `reasons` (JSON) and a
  score. MusicBrainz supplies release groups, labels, and artist genres;
  Internet Archive supplies audio items and torrent URLs. The licence module
  converts licence URLs/rights text into an explicit verdict; anything unclear
  is `unknown` and requires review.
- **Acquisition is rights-gated twice.** Approval refuses releases without
  `redistribution_allowed = 1`, with `needs_manual_review = 1`, or with an
  unknown authorisation. Queueing re-checks the same fields and disk space.
- **Validation is device-specific.** `audio/probe.py` parses FLAC STREAMINFO
  (block sizes, depth, rate, channels, duration) without FFmpeg, then augments
  with `ffprobe` when available, or mutagen container properties as a clearly
  labelled fallback. `audio/device_profile.py` encodes the Echo
  Mini limits (24/192 PCM, DSD64-256, 4608 max FLAC block size, stereo, no
  Opus/DTS/SACD), and `audio/validate.py` adds transcode heuristics
  (upsampled-looking spectra, improbable bitrates, truncation).
- **Organisation is deterministic.** `metadata/classification.py` maps genre
  keywords to the category/subgenre folders; names are sanitised for the
  device, track numbers become `NN - Title.ext`, multi-disc releases get
  `Disc N`. Files are recorded in `files` with checksums and `organised_at`.
- **Sync is explicit and reversible in spirit.** Selection profiles produce a
  file list from `files` joined to tracks. The planner maps each file to
  `Music/<relative path>`, compares size/SHA-256 against the card, and classifies
  each item as `add`, `update`, `skip`, or `no_space`. Execution copies to a
  hidden `.part` file, fsyncs, re-hashes, and `os.replace`s into place. It never
  deletes, and it reports unmanaged card files. Every run is recorded in
  `sync_runs`/`sync_items`.
- **Device identity is user-confirmed.** `device/detect.py` parses `diskutil`
  plists, ignores disk images, and classifies candidates by filesystem,
  capacity, and name. The chosen card is stored in `devices` with
  `confirmed = 1`; sync refuses unconfirmed volumes.
- **Automation is resumable.** `workflow_runs` records start/finish per job;
  schedules (`hourly`, `daily`, `weekly`, `30m`, `6h`, `2d`, `manual`) determine
  due jobs. The launchd agent simply calls `musicctl automation run`.
- **Dashboard is read-only.** FastAPI app with a localhost middleware (client
  address and Host header checks). No mutation endpoints; writes stay in the CLI.

## Failure handling

- Network clients retry on 429/5xx with backoff, refresh on 401, and raise
  `SpotifyUnavailable` for removed/forbidden endpoints so a single unavailable
  resource does not abort an import.
- Missing FFmpeg/qBittorrent produce actionable errors, not crashes. FLAC-only
  workflows remain fully functional.
- Download post-processing validates every file; invalid files are reported,
  unmatched files are copied aside, and failures are recorded per acquisition.
- Sync errors are recorded per item and summarised; successful items remain
  verified.

## Extension points

- Add sources in `discovery/` and seed a row in `sources`.
- Add device profiles in `audio/device_profile.py` if the target player changes.
- Add selection profiles in `sync/selection.py`.
- Add automation jobs in `automation/scheduler.py` + `AutomationConfig`.
