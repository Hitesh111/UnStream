# TODO: FiiO Library Manager

## Phase 1: Environment & Foundation Setup [COMPLETED]
- [x] Inspect Mac environment (Apple Silicon, macOS 15, Python 3.12, storage).
- [x] Identify connected microSD card (`/Volumes/sandisk64`, 63.8GB, ExFAT).
- [x] Establish Python 3.12 virtual environment (`.venv`) and verify dependencies.
- [x] Research and document 2026 Spotify Web API limits and Echo Mini hardware profile.
- [x] Create project tracking docs (`PROJECT_STATUS.md`, `TECHNICAL_DECISIONS.md`, `TODO.md`).
- [x] Run and pass test suite (24 tests passed).

## Phase 2: Configuration & Device Confirmation [COMPLETED]
- [x] Initialize configuration, directory structures, and database via `musicctl init`.
- [x] Inspect and register target microSD device (`sandisk64`, ExFAT).
- [x] Confirm microSD volume (`musicctl device confirm --device sandisk64`).
- [x] Fix status command confirmation check to display device registration accurately.
- [x] Test non-destructive dry-run sync planner (`musicctl sync --dry-run`).
- [x] Verify report generation (`musicctl report`) and discovery source listing.

## Phase 3: Spotify Integration & Taste Profile [COMPLETED]
- [x] Receive and configure Spotify Developer App Client ID.
- [x] Authenticate via PKCE flow (`musicctl spotify login`).
- [x] Securely store tokens in macOS Keychain (`security` CLI).
- [x] Perform incremental library and playlist fetch (`musicctl spotify sync`).
- [x] Generate taste profile scoring and era breakdown (`musicctl spotify profile`).

## Phase 4: Discovery, Rights-Gating & Acquisitions [COMPLETED]
- [x] Query MusicBrainz and Internet Archive candidates based on taste profile.
- [x] Enforce strict license/authorisation filter (only public domain / CC / authorised).
- [x] Verify candidate approval queue (`musicctl queue show`).

## Phase 5: Audio Validation & Organization [COMPLETED]
- [x] Probe audio tracks for FLAC block size <= 4608, bit depth, sample rate, stereo channels.
- [x] Validate embedded baseline JPEG artwork and Vorbis comment metadata.
- [x] Match scanned audio files against Spotify library (`TrackMatcher`).
- [x] Execute non-destructive organization into `Music/<Category>/<Subgenre>/<Artist>/<Album>/`.

## Phase 6: MicroSD Sync & Local Dashboard [COMPLETED]
- [x] Run dry-run sync planner with capacity reserve calculation (`musicctl sync --dry-run`).
- [x] Verify atomic `.part` staging and zero-deletion safety model.
- [x] Verify local read-only dashboard endpoints (`/api/status`, `/api/devices`, `/api/library`).
