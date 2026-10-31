"""FiiO SNOWSKY Echo Mini compatibility rules.

Sources: FiiO ECHO MINI parameter page (FLAC/WAV/APE up to 24-bit/192 kHz,
MP3/OGG/M4A/WMA, DSD64-256, microSD up to 256 GB, NTFS unsupported) and the
community firmware analysis in snowsky-echo-mini-toolbox (max FLAC block size
4608, stereo only, embedded artwork only, filenames displayed by the library).
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any

DEVICE_NAME = "SNOWSKY Echo Mini"

MAX_SAMPLE_RATE = 192_000
MAX_BIT_DEPTH = 24
MAX_CHANNELS = 2
MAX_FLAC_BLOCKSIZE = 4608
MAX_CARD_BYTES = 256 * 1024**3
ARTWORK_RECOMMENDED_PX = 500
ARTWORK_MAX_PX = 2000

SUPPORTED_EXTENSIONS = {".flac", ".mp3", ".ogg", ".m4a", ".aac", ".wav", ".ape", ".dsf", ".dff", ".wma"}
SUPPORTED_CODECS = {
    "flac",
    "mp3",
    "aac",
    "alac",
    "vorbis",
    "ogg",
    "opus",
    "wav",
    "pcm_s16le",
    "pcm_s24le",
    "pcm_s32le",
    "ape",
    "dsd_lsbf",
    "dsd_msbf",
    "wmav1",
    "wmav2",
    "wmalossless",
    "wmapro",
}
DSD_CODECS = {"dsd", "dsd_lsbf", "dsd_msbf"}
DSD_SAMPLE_RATES = {2_822_400, 5_644_800, 11_289_600}
BLOCKED_CODECS = {"opus"}  # Echo Mini hides Opus files entirely
BLOCKED_FORMATS = {"dts", "sacd"}

ILLEGAL_PATH_CHARS = re.compile(r'[\x00-\x1f<>:"/\\|?*~]')
MULTI_SPACE = re.compile(r"\s+")


@dataclass
class Compatibility:
    ok: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "errors": self.errors, "warnings": self.warnings}


def compatibility(probe: Any) -> Compatibility:
    """Check an AudioProbe-like object against Echo Mini limits."""
    errors: list[str] = []
    warnings: list[str] = []

    codec = (getattr(probe, "codec", None) or "").lower()
    format_name = (getattr(probe, "format_name", None) or "").lower()

    if codec in BLOCKED_CODECS or any(blocked in format_name for blocked in BLOCKED_FORMATS):
        errors.append(f"{codec or format_name} is not supported by the {DEVICE_NAME}")

    is_dsd = codec in DSD_CODECS or format_name in {"dsf", "dff"} or "dsd" in format_name
    sample_rate = getattr(probe, "sample_rate", None)
    if sample_rate:
        if is_dsd:
            if sample_rate not in DSD_SAMPLE_RATES:
                warnings.append(
                    f"DSD rate {sample_rate} Hz is unusual; the device supports DSD64/128/256"
                )
        elif sample_rate > MAX_SAMPLE_RATE:
            errors.append(f"Sample rate {sample_rate} Hz exceeds the device maximum ({MAX_SAMPLE_RATE} Hz)")
        elif sample_rate not in (44_100, 48_000, 88_200, 96_000, 176_400, 192_000):
            warnings.append(f"Unusual sample rate {sample_rate} Hz for the device")

    bit_depth = getattr(probe, "bit_depth", None)
    if bit_depth and not is_dsd and bit_depth > MAX_BIT_DEPTH:
        errors.append(f"Bit depth {bit_depth}-bit exceeds the device maximum ({MAX_BIT_DEPTH}-bit)")

    channels = getattr(probe, "channels", None)
    if channels and channels > MAX_CHANNELS:
        errors.append(f"{channels} channels detected; the device is stereo only")

    if codec == "flac":
        max_block = getattr(probe, "flac_max_blocksize", None)
        if max_block and max_block > MAX_FLAC_BLOCKSIZE:
            errors.append(
                f"FLAC block size {max_block} exceeds {MAX_FLAC_BLOCKSIZE}; "
                "the device may silently skip this file"
            )
        if getattr(probe, "has_id3_prefix", False):
            warnings.append("FLAC file has an ID3 tag header; the device ignores ID3 in FLAC files")

    tags = getattr(probe, "tags", None) or {}
    track_number = str(tags.get("track", "")) or str(tags.get("tracknumber", ""))
    if "/" in track_number:
        warnings.append('Compound track number (e.g. "3/10") is not parsed correctly by the device')

    return Compatibility(ok=not errors, errors=errors, warnings=warnings)


def sanitize_component(value: str, max_length: int = 120) -> str:
    """Return a filesystem-safe, device-friendly path component."""
    value = unicodedata.normalize("NFC", value or "").strip()
    value = value.replace("…", "...")
    value = ILLEGAL_PATH_CHARS.sub("", value)
    value = MULTI_SPACE.sub(" ", value).strip(" .")
    if not value:
        value = "Unknown"
    if len(value.encode("utf-8")) > max_length:
        encoded = value.encode("utf-8")[:max_length]
        value = encoded.decode("utf-8", errors="ignore").strip() or "Unknown"
    return value


def sanitize_track_number(track_number: int | str | None) -> str:
    if track_number is None or track_number == "":
        return "00"
    if isinstance(track_number, str) and "/" in track_number:
        track_number = track_number.split("/", 1)[0]
    try:
        number = int(str(track_number).strip())
    except (TypeError, ValueError):
        return "00"
    return f"{number:02d}"


def device_filename(track_number: int | str | None, title: str, extension: str) -> str:
    number = sanitize_track_number(track_number)
    clean_title = sanitize_component(title)
    ext = extension if extension.startswith(".") else f".{extension}"
    return f"{number} - {clean_title}{ext.lower()}"


def artwork_guidance() -> dict[str, Any]:
    return {
        "embedded_only": True,
        "format": "baseline JPEG",
        "recommended_px": ARTWORK_RECOMMENDED_PX,
        "max_px": ARTWORK_MAX_PX,
        "notes": "Sidecar cover.jpg/folder.jpg files are ignored; progressive JPEG may not render.",
    }
