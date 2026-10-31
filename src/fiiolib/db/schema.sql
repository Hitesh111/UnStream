PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS schema_migrations (
    version     INTEGER PRIMARY KEY,
    applied_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS settings (
    key        TEXT PRIMARY KEY,
    value      TEXT,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS artists (
    id             INTEGER PRIMARY KEY,
    spotify_id     TEXT UNIQUE,
    musicbrainz_id TEXT,
    name           TEXT NOT NULL,
    sort_name      TEXT,
    image_url      TEXT,
    source         TEXT NOT NULL DEFAULT 'spotify',
    created_at     TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at     TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_artists_name ON artists(name COLLATE NOCASE);
CREATE INDEX IF NOT EXISTS idx_artists_mbid ON artists(musicbrainz_id);

CREATE TABLE IF NOT EXISTS labels (
    id             INTEGER PRIMARY KEY,
    name           TEXT NOT NULL UNIQUE COLLATE NOCASE,
    musicbrainz_id TEXT,
    discogs_id     TEXT,
    created_at     TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS albums (
    id                        INTEGER PRIMARY KEY,
    spotify_id                TEXT UNIQUE,
    musicbrainz_release_group TEXT,
    musicbrainz_release       TEXT,
    title                     TEXT NOT NULL,
    artist_id                 INTEGER REFERENCES artists(id) ON DELETE SET NULL,
    album_type                TEXT,
    release_date              TEXT,
    release_year              INTEGER,
    label_id                  INTEGER REFERENCES labels(id) ON DELETE SET NULL,
    total_tracks              INTEGER,
    cover_url                 TEXT,
    source                    TEXT NOT NULL DEFAULT 'spotify',
    created_at                TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at                TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_albums_artist ON albums(artist_id);
CREATE INDEX IF NOT EXISTS idx_albums_mbid ON albums(musicbrainz_release_group);
CREATE INDEX IF NOT EXISTS idx_albums_title ON albums(title COLLATE NOCASE);

CREATE TABLE IF NOT EXISTS tracks (
    id                       INTEGER PRIMARY KEY,
    spotify_id               TEXT UNIQUE,
    isrc                     TEXT,
    musicbrainz_recording_id TEXT,
    title                    TEXT NOT NULL,
    artist_id                INTEGER REFERENCES artists(id) ON DELETE SET NULL,
    album_id                 INTEGER REFERENCES albums(id) ON DELETE SET NULL,
    disc_number              INTEGER DEFAULT 1,
    track_number             INTEGER DEFAULT 0,
    duration_ms              INTEGER,
    explicit                 INTEGER DEFAULT 0,
    source_url               TEXT,
    created_at               TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at               TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_tracks_artist ON tracks(artist_id);
CREATE INDEX IF NOT EXISTS idx_tracks_album ON tracks(album_id);
CREATE INDEX IF NOT EXISTS idx_tracks_isrc ON tracks(isrc);
CREATE INDEX IF NOT EXISTS idx_tracks_mbid ON tracks(musicbrainz_recording_id);

CREATE TABLE IF NOT EXISTS genres (
    id        INTEGER PRIMARY KEY,
    name      TEXT NOT NULL UNIQUE COLLATE NOCASE,
    parent_id INTEGER REFERENCES genres(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS album_artists (
    album_id  INTEGER NOT NULL REFERENCES albums(id) ON DELETE CASCADE,
    artist_id INTEGER NOT NULL REFERENCES artists(id) ON DELETE CASCADE,
    position  INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (album_id, artist_id)
);

CREATE TABLE IF NOT EXISTS track_artists (
    track_id  INTEGER NOT NULL REFERENCES tracks(id) ON DELETE CASCADE,
    artist_id INTEGER NOT NULL REFERENCES artists(id) ON DELETE CASCADE,
    position  INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (track_id, artist_id)
);

CREATE TABLE IF NOT EXISTS track_genres (
    track_id INTEGER NOT NULL REFERENCES tracks(id) ON DELETE CASCADE,
    genre_id INTEGER NOT NULL REFERENCES genres(id) ON DELETE CASCADE,
    weight   REAL NOT NULL DEFAULT 1.0,
    source   TEXT NOT NULL DEFAULT 'spotify',
    PRIMARY KEY (track_id, genre_id)
);
CREATE TABLE IF NOT EXISTS album_genres (
    album_id INTEGER NOT NULL REFERENCES albums(id) ON DELETE CASCADE,
    genre_id INTEGER NOT NULL REFERENCES genres(id) ON DELETE CASCADE,
    weight   REAL NOT NULL DEFAULT 1.0,
    source   TEXT NOT NULL DEFAULT 'spotify',
    PRIMARY KEY (album_id, genre_id)
);
CREATE TABLE IF NOT EXISTS artist_genres (
    artist_id INTEGER NOT NULL REFERENCES artists(id) ON DELETE CASCADE,
    genre_id  INTEGER NOT NULL REFERENCES genres(id) ON DELETE CASCADE,
    weight    REAL NOT NULL DEFAULT 1.0,
    source    TEXT NOT NULL DEFAULT 'spotify',
    PRIMARY KEY (artist_id, genre_id)
);

CREATE TABLE IF NOT EXISTS playlists (
    id            INTEGER PRIMARY KEY,
    spotify_id    TEXT UNIQUE,
    name          TEXT NOT NULL,
    owner_id      TEXT,
    owner_name    TEXT,
    description   TEXT,
    public        INTEGER,
    collaborative INTEGER DEFAULT 0,
    snapshot_id   TEXT,
    kind          TEXT NOT NULL DEFAULT 'personal'
        CHECK (kind IN ('personal', 'followed', 'collaborative', 'seed', 'unknown')),
    total_tracks  INTEGER DEFAULT 0,
    last_synced_at TEXT,
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS playlist_tracks (
    playlist_id INTEGER NOT NULL REFERENCES playlists(id) ON DELETE CASCADE,
    track_id    INTEGER NOT NULL REFERENCES tracks(id) ON DELETE CASCADE,
    position    INTEGER NOT NULL DEFAULT 0,
    added_at    TEXT,
    added_by    TEXT,
    PRIMARY KEY (playlist_id, track_id, position)
);
CREATE INDEX IF NOT EXISTS idx_playlist_tracks_track ON playlist_tracks(track_id);

CREATE TABLE IF NOT EXISTS spotify_saved_tracks (
    track_id  INTEGER PRIMARY KEY REFERENCES tracks(id) ON DELETE CASCADE,
    added_at  TEXT,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS spotify_saved_albums (
    album_id  INTEGER PRIMARY KEY REFERENCES albums(id) ON DELETE CASCADE,
    added_at  TEXT,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS spotify_followed_artists (
    artist_id  INTEGER PRIMARY KEY REFERENCES artists(id) ON DELETE CASCADE,
    captured_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS spotify_recently_played (
    track_id   INTEGER NOT NULL REFERENCES tracks(id) ON DELETE CASCADE,
    played_at  TEXT NOT NULL,
    context_uri TEXT,
    PRIMARY KEY (track_id, played_at)
);

CREATE TABLE IF NOT EXISTS spotify_top_items (
    id          INTEGER PRIMARY KEY,
    entity_type TEXT NOT NULL CHECK (entity_type IN ('artist', 'track')),
    entity_id   INTEGER NOT NULL,
    time_range  TEXT NOT NULL,
    rank        INTEGER NOT NULL,
    captured_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_top_items_capture ON spotify_top_items(entity_type, time_range, captured_at);

CREATE TABLE IF NOT EXISTS spotify_imports (
    id         INTEGER PRIMARY KEY,
    kind       TEXT NOT NULL,
    started_at TEXT NOT NULL DEFAULT (datetime('now')),
    finished_at TEXT,
    status     TEXT NOT NULL DEFAULT 'running',
    items      INTEGER DEFAULT 0,
    error      TEXT,
    summary    TEXT
);

CREATE TABLE IF NOT EXISTS spotify_snapshots (
    id          INTEGER PRIMARY KEY,
    captured_at TEXT NOT NULL DEFAULT (datetime('now')),
    payload     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS discovery_candidates (
    id                INTEGER PRIMARY KEY,
    entity_type       TEXT NOT NULL CHECK (entity_type IN ('artist', 'album', 'track')),
    entity_spotify_id TEXT,
    entity_name       TEXT NOT NULL,
    artist_id         INTEGER REFERENCES artists(id) ON DELETE SET NULL,
    album_id          INTEGER REFERENCES albums(id) ON DELETE SET NULL,
    score             REAL NOT NULL DEFAULT 0,
    reasons           TEXT NOT NULL DEFAULT '[]',
    status            TEXT NOT NULL DEFAULT 'discovered'
        CHECK (status IN ('discovered', 'awaiting_approval', 'approved', 'queued',
                          'downloading', 'downloaded', 'verified', 'organised',
                          'synced', 'failed', 'rejected')),
    notes             TEXT,
    reviewed_at       TEXT,
    created_at        TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at        TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (entity_type, entity_spotify_id)
);
CREATE INDEX IF NOT EXISTS idx_candidates_status ON discovery_candidates(status, score DESC);

CREATE TABLE IF NOT EXISTS sources (
    id        INTEGER PRIMARY KEY,
    name      TEXT NOT NULL UNIQUE,
    kind      TEXT NOT NULL,
    base_url  TEXT,
    terms_url TEXT,
    enabled   INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS releases (
    id                      INTEGER PRIMARY KEY,
    source_id               INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    external_id             TEXT NOT NULL,
    title                   TEXT NOT NULL,
    artist_name             TEXT,
    album_id                INTEGER REFERENCES albums(id) ON DELETE SET NULL,
    candidate_id            INTEGER REFERENCES discovery_candidates(id) ON DELETE SET NULL,
    media_type              TEXT,
    quality                 TEXT,
    size_bytes              INTEGER,
    download_url            TEXT,
    licence                 TEXT,
    licence_url             TEXT,
    redistribution_allowed  INTEGER,
    authorisation           TEXT NOT NULL DEFAULT 'unknown'
        CHECK (authorisation IN ('artist_authorised', 'label_authorised', 'public_domain',
                                 'creative_commons', 'official_free', 'unknown')),
    needs_manual_review     INTEGER NOT NULL DEFAULT 1,
    verified_at             TEXT,
    metadata                TEXT,
    created_at              TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (source_id, external_id)
);

CREATE TABLE IF NOT EXISTS acquisitions (
    id           INTEGER PRIMARY KEY,
    release_id   INTEGER REFERENCES releases(id) ON DELETE CASCADE,
    candidate_id INTEGER REFERENCES discovery_candidates(id) ON DELETE SET NULL,
    status       TEXT NOT NULL DEFAULT 'awaiting_approval'
        CHECK (status IN ('awaiting_approval', 'approved', 'queued', 'downloading',
                          'downloaded', 'verified', 'organised', 'failed', 'rejected')),
    approved_at  TEXT,
    approved_by  TEXT,
    notes        TEXT,
    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS torrent_jobs (
    id               INTEGER PRIMARY KEY,
    acquisition_id   INTEGER REFERENCES acquisitions(id) ON DELETE CASCADE,
    qbit_hash        TEXT UNIQUE,
    name             TEXT,
    magnet           TEXT,
    save_path        TEXT,
    status           TEXT NOT NULL DEFAULT 'queued',
    progress         REAL DEFAULT 0,
    downloaded_bytes INTEGER DEFAULT 0,
    total_bytes      INTEGER DEFAULT 0,
    error            TEXT,
    created_at       TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at       TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS files (
    id               INTEGER PRIMARY KEY,
    track_id         INTEGER REFERENCES tracks(id) ON DELETE SET NULL,
    path             TEXT NOT NULL UNIQUE,
    size_bytes       INTEGER,
    mtime            REAL,
    sha256           TEXT,
    format           TEXT,
    codec            TEXT,
    bit_depth        INTEGER,
    sample_rate      INTEGER,
    channels         INTEGER,
    bitrate          INTEGER,
    duration_ms      INTEGER,
    validated        INTEGER NOT NULL DEFAULT 0,
    validation_notes TEXT,
    transcode_suspect INTEGER NOT NULL DEFAULT 0,
    organised_at     TEXT,
    created_at       TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at       TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_files_sha ON files(sha256);
CREATE INDEX IF NOT EXISTS idx_files_track ON files(track_id);

CREATE TABLE IF NOT EXISTS audio_validations (
    id         INTEGER PRIMARY KEY,
    file_id    INTEGER REFERENCES files(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    ok         INTEGER NOT NULL DEFAULT 0,
    findings   TEXT,
    probe      TEXT
);

CREATE TABLE IF NOT EXISTS licences (
    id                    INTEGER PRIMARY KEY,
    code                  TEXT NOT NULL UNIQUE,
    name                  TEXT NOT NULL,
    url                   TEXT,
    allows_redistribution INTEGER NOT NULL DEFAULT 0,
    notes                 TEXT
);

CREATE TABLE IF NOT EXISTS devices (
    id             INTEGER PRIMARY KEY,
    identifier     TEXT NOT NULL UNIQUE,
    volume_name    TEXT,
    mount_point    TEXT,
    filesystem     TEXT,
    capacity_bytes INTEGER,
    is_echo_mini   INTEGER NOT NULL DEFAULT 0,
    confirmed      INTEGER NOT NULL DEFAULT 0,
    last_seen_at   TEXT,
    notes          TEXT
);

CREATE TABLE IF NOT EXISTS sync_runs (
    id            INTEGER PRIMARY KEY,
    started_at    TEXT NOT NULL DEFAULT (datetime('now')),
    finished_at   TEXT,
    device_id     INTEGER REFERENCES devices(id) ON DELETE SET NULL,
    dry_run       INTEGER NOT NULL DEFAULT 1,
    status        TEXT NOT NULL DEFAULT 'planned',
    planned_bytes INTEGER DEFAULT 0,
    copied_bytes  INTEGER DEFAULT 0,
    added         INTEGER DEFAULT 0,
    updated       INTEGER DEFAULT 0,
    removed       INTEGER DEFAULT 0,
    skipped       INTEGER DEFAULT 0,
    errors        TEXT
);

CREATE TABLE IF NOT EXISTS sync_items (
    id          INTEGER PRIMARY KEY,
    sync_run_id INTEGER NOT NULL REFERENCES sync_runs(id) ON DELETE CASCADE,
    file_id     INTEGER REFERENCES files(id) ON DELETE SET NULL,
    action      TEXT NOT NULL,
    src_path    TEXT,
    dest_path   TEXT,
    bytes       INTEGER,
    checksum    TEXT,
    status      TEXT NOT NULL DEFAULT 'planned',
    error       TEXT
);

CREATE TABLE IF NOT EXISTS user_preferences (
    key        TEXT PRIMARY KEY,
    value      TEXT,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS logs (
    id         INTEGER PRIMARY KEY,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    level      TEXT NOT NULL,
    component  TEXT,
    message    TEXT NOT NULL,
    context    TEXT
);

CREATE TABLE IF NOT EXISTS workflow_runs (
    id           INTEGER PRIMARY KEY,
    workflow     TEXT NOT NULL,
    started_at   TEXT NOT NULL DEFAULT (datetime('now')),
    finished_at  TEXT,
    status       TEXT NOT NULL DEFAULT 'running',
    resume_token TEXT,
    summary      TEXT,
    error        TEXT
);
CREATE INDEX IF NOT EXISTS idx_workflow_status ON workflow_runs(workflow, status);

CREATE TABLE IF NOT EXISTS dedupe_actions (
    id         INTEGER PRIMARY KEY,
    file_id    INTEGER REFERENCES files(id) ON DELETE CASCADE,
    duplicate_of INTEGER REFERENCES files(id) ON DELETE SET NULL,
    action     TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

INSERT OR IGNORE INTO sources (name, kind, base_url, terms_url) VALUES
    ('internet_archive', 'archive', 'https://archive.org', 'https://archive.org/about/terms.php'),
    ('jamendo', 'cc_catalogue', 'https://www.jamendo.com', 'https://www.jamendo.com/legal/terms'),
    ('free_music_archive', 'cc_catalogue', 'https://freemusicarchive.org', 'https://freemusicarchive.org/terms/'),
    ('bandcamp', 'artist_store', 'https://bandcamp.com', 'https://bandcamp.com/terms_of_use');

INSERT OR IGNORE INTO licences (code, name, url, allows_redistribution) VALUES
    ('public_domain', 'Public Domain', 'https://creativecommons.org/publicdomain/mark/1.0/', 1),
    ('cc0', 'Creative Commons Zero', 'https://creativecommons.org/publicdomain/zero/1.0/', 1),
    ('cc_by', 'Creative Commons Attribution', 'https://creativecommons.org/licenses/by/4.0/', 1),
    ('cc_by_sa', 'Creative Commons Attribution-ShareAlike', 'https://creativecommons.org/licenses/by-sa/4.0/', 1),
    ('cc_by_nc', 'Creative Commons Attribution-NonCommercial', 'https://creativecommons.org/licenses/by-nc/4.0/', 1),
    ('cc_by_nc_sa', 'Creative Commons Attribution-NonCommercial-ShareAlike', 'https://creativecommons.org/licenses/by-nc-sa/4.0/', 1),
    ('cc_by_nd', 'Creative Commons Attribution-NoDerivatives', 'https://creativecommons.org/licenses/by-nd/4.0/', 0),
    ('cc_sampling', 'Creative Commons Sampling Plus', 'https://creativecommons.org/licenses/sampling+/1.0/', 1),
    ('all_rights_reserved', 'All Rights Reserved', NULL, 0),
    ('unknown', 'Unknown / unverified', NULL, 0);
