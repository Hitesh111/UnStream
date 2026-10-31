# Limitations and caveats

## Environment (this machine)

- `ffmpeg`/`ffprobe` are **not installed**. FLAC probing/validation works from
  STREAMINFO, and a mutagen fallback reads container properties for
  WAV/OGG/MP3/M4A/DSF. APE probing, artwork conversion, and full authoritative
  verification still need FFmpeg (`brew install ffmpeg`).
- qBittorrent is **not installed**. Download queueing/polling code paths return
  actionable `QBittorrentError`s. Everything else works without it.
- The card (`sandisk64`, ExFAT, ~59 GiB free) must be explicitly confirmed
  before a non-dry-run sync. No data was written to it during development.

## Spotify Web API (February 2026 development mode)

- The app owner must have an active Premium subscription; apps are limited to
  one Client ID and five users.
- Batch endpoints (`/albums?ids=`, `/tracks?ids=`, `/artists?ids=`) are gone;
  the client fetches resources individually with rate limiting and caching.
- Browse new-releases/categories are removed; discovery relies on saved items,
  playlists, follows, top items, recent plays, and seed playlists.
- Search is limited to 10 results per request.
- Playlist tracks moved to `items`, and items are only returned for playlists
  you own or collaborate on. Others may appear empty.
- `popularity`, follower counts, and album `label` were removed.
- ISRC is available again (March 2026 changelog) and is used for exact matching
  when present.
- The redirect URI must match exactly. PKCE means no client secret is stored;
  tokens live in Keychain. Logging out removes them.

## Echo Mini

- Supports FLAC/WAV/APE up to 24-bit/192 kHz, MP3/OGG/M4A/WMA, DSD64-256.
  SACD and DTS are not supported; Opus files are hidden by the firmware.
- microSD only, up to 256 GB; **NTFS is not supported**. Use ExFAT/FAT32.
- Maximum FLAC block size is 4608. Files above it may be skipped silently; the
  validator rejects them.
- Stereo only. Embedded **baseline JPEG** artwork is required; sidecar
  `cover.jpg`/`folder.jpg` is ignored, and progressive JPEG may not render.
- The library displays filenames, so `NN - Title.ext` naming is applied.
- Compound track numbers (`3/10`) are not parsed correctly; the sanitiser
  strips the total.
- ID3 tags inside FLAC are ignored; Vorbis comments are written instead.
- Firmware behaviour can change between releases; re-run `musicctl validate`
  after a firmware update.

## Match quality

- Track matching uses ISRC when available, then normalised title/artist/album
  plus duration similarity. Ambiguous matches are rejected rather than forced.
- Multi-disc and various-artists releases depend on correct album/track
  metadata from the source; unmatched files are copied to
  `verified/unmatched-<id>/` for manual review.
- Classical/compilation metadata is often inconsistent across sources; expect
  more manual review there.

## Rights and acquisitions

- Only explicit public-domain, Creative Commons, artist/label-authorised, or
  officially free releases are eligible for automatic approval.
- "Free to stream" or "available on the artist's site" is not treated as a
  download authorisation unless the source states it.
- Torrent availability, seeds, and legal status vary by release; approval is a
  rights gate, not a guarantee of downloadability.
- This tool does not bypass DRM, paywalls, or access controls, and does not
  download from streaming services.

## Sync

- Sync never deletes files on the card, including stale tracks. Remove obsolete
  files manually after reviewing the **unmanaged** list.
- `--fast` skips destination hashing and treats equal-size files as unchanged.
  Use it only when you trust size equality; the default verifies SHA-256.
- The capacity reserve defaults to 1.5 GB. Very small cards or large single
  files can still produce `no_space` items; the plan reports them.
- macOS may create `._*` AppleDouble files on some filesystems; these are
  ignored by scans/sync and not deleted.

## Dashboard and automation

- The dashboard is read-only and localhost-only; it is not a remote control
  plane. Do not expose it through a reverse proxy.
- launchd runs only while the user is logged in. Missed runs execute at the
  next check; jobs are idempotent.
- Card auto-sync is disabled by default (`device.auto_sync: false`). Enabling it
  still requires a confirmed card.
