# TODO: FiiO Library Manager

## Phase 1: Environment & Foundation Setup [COMPLETED]
- [x] Inspect Mac environment (Apple Silicon, macOS 15, Python 3.12, storage).
- [x] Identify connected microSD card (`/Volumes/sandisk64`, 63.8GB, ExFAT).
- [x] Establish Python 3.12 virtual environment (`.venv`) and verify dependencies.
- [x] Research and document 2026 Spotify Web API limits and Echo Mini hardware profile.
- [x] Create project tracking docs (`PROJECT_STATUS.md`, `TECHNICAL_DECISIONS.md`, `TODO.md`).
- [x] Run and pass test suite (24 tests passed).

## Phase 2: Configuration & Device Detection
- [ ] Initialize configuration via `musicctl init` (or generate custom test config).
- [ ] Test read-only device detection CLI command: `musicctl device detect`.
- [ ] Verify non-destructive dry-run behaviors across all commands.

## Phase 3: Spotify Integration & Taste Profile
- [ ] Prompt user for Spotify Developer App Client ID.
- [ ] Authenticate via PKCE flow (`musicctl spotify login`).
- [ ] Perform incremental library and playlist fetch (`musicctl spotify sync`).
- [ ] Generate taste profile scoring and genre mapping (`musicctl spotify profile`).

## Phase 4: Discovery, Rights-Gating & Acquisitions
- [ ] Query MusicBrainz and Internet Archive candidates based on taste profile.
- [ ] Enforce strict license/authorisation filter (only public domain / CC / authorised).
- [ ] Review approval queue via `musicctl queue list` and `musicctl queue approve`.

## Phase 5: Audio Validation & Organization
- [ ] Probe audio tracks for FLAC block size <= 4608, bit depth, sample rate, stereo channels.
- [ ] Validate embedded baseline JPEG artwork and Vorbis comment metadata.
- [ ] Execute non-destructive organization into `Music/<Category>/<Subgenre>/<Artist>/<Album>/`.

## Phase 6: MicroSD Sync & Local Dashboard
- [ ] Run dry-run sync planner with capacity reserve calculation.
- [ ] Perform incremental checksum-verified sync with `.part` staging upon confirmation.
- [ ] Launch local read-only dashboard (`musicctl dashboard`) on `127.0.0.1:8080`.
