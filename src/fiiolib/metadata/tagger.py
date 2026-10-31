"""Tag writing with Mutagen, tuned for the Echo Mini (Vorbis comments, plain track numbers)."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class TrackMetadata:
    title: str
    artist: str
    album: str
    album_artist: str | None = None
    genre: str | None = None
    track_number: int | None = None
    disc_number: int | None = 1
    year: str | None = None
    label: str | None = None
    isrc: str | None = None
    musicbrainz_recording_id: str | None = None
    musicbrainz_release_id: str | None = None
    source_url: str | None = None
    licence: str | None = None
    extra: dict[str, str] = field(default_factory=dict)


def _jpg_or_png(data: bytes | None) -> tuple[bytes | None, str | None]:
    if not data:
        return None, None
    if data[:3] == b"\xff\xd8\xff":
        return data, "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return data, "image/png"
    return None, None


def write_tags(
    path: str | Path,
    metadata: TrackMetadata,
    artwork: bytes | None = None,
    preserve_existing: bool = True,
) -> list[str]:
    """Write tags, returning human-readable notes about anything skipped."""
    file_path = Path(path)
    suffix = file_path.suffix.lower()
    notes: list[str] = []
    if suffix == ".flac":
        notes.extend(_write_flac(file_path, metadata, artwork, preserve_existing))
    elif suffix == ".mp3":
        notes.extend(_write_id3(file_path, metadata, artwork, preserve_existing))
    elif suffix in {".m4a", ".aac", ".mp4"}:
        notes.extend(_write_mp4(file_path, metadata, artwork, preserve_existing))
    elif suffix == ".ogg":
        notes.extend(_write_ogg(file_path, metadata, artwork))
    else:
        notes.append(f"Tag writing not implemented for {suffix}; file left untouched")
    return notes


def _write_flac(path: Path, metadata: TrackMetadata, artwork: bytes | None, preserve: bool) -> list[str]:
    from mutagen.flac import FLAC, Picture

    notes: list[str] = []
    audio = FLAC(str(path))
    tags = {
        "TITLE": metadata.title,
        "ARTIST": metadata.artist,
        "ALBUM": metadata.album,
        "ALBUMARTIST": metadata.album_artist or metadata.artist,
    }
    if metadata.genre:
        tags["GENRE"] = metadata.genre
    if metadata.track_number:
        tags["TRACKNUMBER"] = str(int(metadata.track_number))
    if metadata.disc_number:
        tags["DISCNUMBER"] = str(int(metadata.disc_number))
    if metadata.year:
        tags["DATE"] = metadata.year
    if metadata.label:
        tags["LABEL"] = metadata.label
    if metadata.isrc:
        tags["ISRC"] = metadata.isrc
    if metadata.musicbrainz_recording_id:
        tags["MUSICBRAINZ_TRACKID"] = metadata.musicbrainz_recording_id
    if metadata.musicbrainz_release_id:
        tags["MUSICBRAINZ_ALBUMID"] = metadata.musicbrainz_release_id
    if metadata.licence:
        tags["LICENSE"] = metadata.licence
    if metadata.source_url:
        tags["SOURCE"] = metadata.source_url
    for key, value in metadata.extra.items():
        if value:
            tags[key.upper()] = value

    if preserve:
        for key, value in tags.items():
            if key in audio and audio.get(key) and not value:
                continue
            audio[key] = value
    else:
        audio.clear_pictures()
        audio.delete()
        for key, value in tags.items():
            audio[key] = value
    audio.save()

    if artwork:
        data, mime = _jpg_or_png(artwork)
        if not data:
            notes.append("Artwork ignored: not JPEG or PNG")
        else:
            if mime != "image/jpeg":
                notes.append("Artwork is PNG; the device prefers baseline JPEG")
            if audio.pictures:
                audio.clear_pictures()
            picture = Picture()
            picture.type = 3
            picture.mime = mime
            picture.desc = "Cover"
            picture.data = data
            audio.add_picture(picture)
            audio.save()
    return notes


def _write_id3(path: Path, metadata: TrackMetadata, artwork: bytes | None, preserve: bool) -> list[str]:
    from mutagen.id3 import (
        APIC,
        ID3,
        TALB,
        TCON,
        TDRC,
        TIT2,
        TPE1,
        TPE2,
        TPOS,
        TPUB,
        TRCK,
        TSRC,
        TXXX,
    )

    notes: list[str] = []
    try:
        tags = ID3(str(path))
    except Exception:
        tags = ID3()
    if not preserve:
        tags.delete()
    tags.setall("TIT2", [TIT2(encoding=3, text=metadata.title)])
    tags.setall("TPE1", [TPE1(encoding=3, text=metadata.artist)])
    tags.setall("TALB", [TALB(encoding=3, text=metadata.album)])
    tags.setall("TPE2", [TPE2(encoding=3, text=metadata.album_artist or metadata.artist)])
    if metadata.genre:
        tags.setall("TCON", [TCON(encoding=3, text=metadata.genre)])
    if metadata.track_number:
        tags.setall("TRCK", [TRCK(encoding=3, text=str(int(metadata.track_number)))])
    if metadata.disc_number:
        tags.setall("TPOS", [TPOS(encoding=3, text=str(int(metadata.disc_number)))])
    if metadata.year:
        tags.setall("TDRC", [TDRC(encoding=3, text=metadata.year)])
    if metadata.label:
        tags.setall("TPUB", [TPUB(encoding=3, text=metadata.label)])
    if metadata.isrc:
        tags.setall("TSRC", [TSRC(encoding=3, text=metadata.isrc)])
    if metadata.source_url:
        tags.add(TXXX(encoding=3, desc="SOURCE", text=metadata.source_url))
    if metadata.licence:
        tags.add(TXXX(encoding=3, desc="LICENSE", text=metadata.licence))
    for key, value in metadata.extra.items():
        if value:
            tags.add(TXXX(encoding=3, desc=key.upper(), text=value))
    if artwork:
        data, mime = _jpg_or_png(artwork)
        if data:
            tags.delall("APIC")
            tags.add(APIC(encoding=3, mime=mime, type=3, desc="Cover", data=data))
        else:
            notes.append("Artwork ignored: not JPEG or PNG")
    tags.save(str(path), v2_version=4)
    return notes


def _write_mp4(path: Path, metadata: TrackMetadata, artwork: bytes | None, preserve: bool) -> list[str]:
    from mutagen.mp4 import MP4, MP4Cover

    notes: list[str] = []
    audio = MP4(str(path))
    tags = audio.tags or {}
    tags["\xa9nam"] = [metadata.title]
    tags["\xa9ART"] = [metadata.artist]
    tags["\xa9alb"] = [metadata.album]
    tags["aART"] = [metadata.album_artist or metadata.artist]
    if metadata.genre:
        tags["\xa9gen"] = [metadata.genre]
    if metadata.track_number:
        tags["trkn"] = [(int(metadata.track_number), 0)]
    if metadata.disc_number:
        tags["disk"] = [(int(metadata.disc_number), 0)]
    if metadata.year:
        tags["\xa9day"] = [metadata.year]
    if metadata.licence:
        tags["----:com.apple.iTunes:LICENSE"] = [metadata.licence.encode()]
    if artwork:
        data, mime = _jpg_or_png(artwork)
        if data:
            tags["covr"] = [MP4Cover(data, imageformat=MP4Cover.FORMAT_JPEG if mime == "image/jpeg" else MP4Cover.FORMAT_PNG)]
    audio.tags = tags
    audio.save()
    return notes


def _write_ogg(path: Path, metadata: TrackMetadata, artwork: bytes | None) -> list[str]:
    from mutagen.oggvorbis import OggVorbis

    audio = OggVorbis(str(path))
    audio["TITLE"] = metadata.title
    audio["ARTIST"] = metadata.artist
    audio["ALBUM"] = metadata.album
    audio["ALBUMARTIST"] = metadata.album_artist or metadata.artist
    if metadata.genre:
        audio["GENRE"] = metadata.genre
    if metadata.track_number:
        audio["TRACKNUMBER"] = str(int(metadata.track_number))
    if metadata.disc_number:
        audio["DISCNUMBER"] = str(int(metadata.disc_number))
    if metadata.year:
        audio["DATE"] = metadata.year
    audio.save()
    return []


def read_tags(path: str | Path) -> dict[str, Any]:
    from mutagen import File as MutagenFile

    audio = MutagenFile(str(path), easy=True)
    if audio is None:
        return {}
    tags = audio.tags or {}
    result: dict[str, Any] = {}
    for key, value in tags.items():
        result[key.lower()] = value[0] if isinstance(value, list) and value else value
    if getattr(audio, "info", None):
        result["_duration_ms"] = int(audio.info.length * 1000)
    return result
