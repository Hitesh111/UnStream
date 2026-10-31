# Technical Decisions

## 1. Storage & Database Layer
- **SQLite with WAL Mode**: We use SQLite with WAL (`PRAGMA journal_mode = WAL`), foreign keys enabled (`PRAGMA foreign_keys = ON`), `isolation_level = None`, and explicit `BEGIN IMMEDIATE` transactions for write operations. This prevents lock contention, ensures ACID compliance, and guarantees zero corruption on unexpected halts.
- **Repository Pattern (`fiiolib.db.repo`)**: Idempotent upsert helpers for artists, albums, tracks, files, candidates, and sync runs. Both Spotify URIs/IDs and MusicBrainz IDs (MBIDs) are preserved to prevent duplicate insertions across sync cycles.

## 2. Spotify Web API (2026 Developer Mode Compliance)
- **PKCE OAuth 2.0**: PKCE without client secret ensures secrets are never baked into source code or config files. Tokens are stored securely in macOS Keychain using `security find-generic-password` / `add-generic-password`, with a fallback to `0600` permissions on disk.
- **Granular Fetching & Polite Rate Limiting**: Since batch endpoints (`/albums?ids=`, etc.) were deprecated in early 2026, the client fetches resources individually with exponential backoff and localized TTL-based caching.
- **Playlist Pagination**: Targets `/v1/playlists/{id}/items` exclusively for owned or collaborative playlists as per the Feb 2026 policy change.

## 3. FiiO SNOWSKY Echo Mini Hardware Profile
- **Audio Validation**:
  - Sample Rates & Bit Depths: PCM up to 24-bit/192kHz, DSD64-256.
  - Channels: Strict stereo (2 channels) only.
  - FLAC Block Size: Capped at 4608 samples. Files exceeding 4608 cause the player to skip playback or hang.
- **Tagging & Artwork Rules**:
  - Vorbis comments for FLAC (ID3 tags inside FLAC are ignored or cause parsing errors in firmware).
  - Embedded baseline JPEG artwork only; progressive JPEG or external sidecar files (`folder.jpg`) are not rendered reliably by the Echo Mini UI.
  - Track numbers formatted as `NN` (compound values like `3/10` stripped to `03`).
- **Path Structure**:
  - Deterministic hierarchy: `Music/<Category>/<Subgenre>/<Artist>/<Album>/<NN> - <Title>.<ext>`.
  - Non-destructive naming sanitization for FAT32/ExFAT character limits and special characters (`/`, `\`, `:`, `*`, `?`, `"`, `<`, `>`, `|`).

## 4. MicroSD & Data Safety Rules
- **No Destruction Ever**:
  - Duplicate detection only flags duplicate reports; it never deletes audio.
  - Library organization copies or moves verified files into destination trees with strict path traversal checks.
  - Sync never purges or prunes unmanaged files on the SD card.
- **Atomic Two-Phase Sync**:
  - Writes are directed to `.part` files first, followed by explicit `flush()` and `os.fsync()`, SHA-256 verification, and atomic rename (`os.replace`).
  - Sync requires explicit device confirmation (`confirmed = 1` in SQLite) and `--yes` flag to prevent unintended writes.
- **Capacity Reserve**:
  - A minimum reserve (default 1.5 GB) is enforced before scheduling any write to avoid filling the FAT32/ExFAT allocation table.

## 5. Pure-Python Fallbacks
- Direct binary parsing of the FLAC `STREAMINFO` block allows fast, 100% reliable hardware limit validation without needing external C/FFmpeg binaries. Mutagen provides fallback tag parsing for WAV, MP3, M4A, and OGG.
