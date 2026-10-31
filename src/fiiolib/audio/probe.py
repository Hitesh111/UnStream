"""Audio probing with ffprobe plus a pure-Python FLAC STREAMINFO reader."""

from __future__ import annotations

import contextlib
import json
import shutil
import struct
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


class AudioToolMissing(RuntimeError):
    pass


@dataclass
class AudioProbe:
    path: str
    size_bytes: int
    format_name: str | None = None
    codec: str | None = None
    bit_depth: int | None = None
    sample_rate: int | None = None
    channels: int | None = None
    bitrate: int | None = None
    duration_ms: int | None = None
    tags: dict[str, str] = field(default_factory=dict)
    flac_min_blocksize: int | None = None
    flac_max_blocksize: int | None = None
    has_id3_prefix: bool = False
    backend: str | None = None
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def find_tool(name: str, configured: str | None = None) -> str | None:
    if configured:
        candidate = Path(configured).expanduser()
        if candidate.is_file():
            return str(candidate)
    return shutil.which(configured or name)


def require_ffprobe(configured: str | None = None) -> str:
    tool = find_tool("ffprobe", configured)
    if not tool:
        raise AudioToolMissing(
            "ffprobe not found. Install FFmpeg (e.g. `brew install ffmpeg`) "
            "or set audio.ffprobe_path in the configuration."
        )
    return tool


def require_ffmpeg(configured: str | None = None) -> str:
    tool = find_tool("ffmpeg", configured)
    if not tool:
        raise AudioToolMissing(
            "ffmpeg not found. Install FFmpeg (e.g. `brew install ffmpeg`) "
            "or set audio.ffmpeg_path in the configuration."
        )
    return tool


def _id3_size(data: bytes) -> int:
    if len(data) < 10 or data[:3] != b"ID3":
        return 0
    b = data[6:10]
    size = ((b[0] & 0x7F) << 21) | ((b[1] & 0x7F) << 14) | ((b[2] & 0x7F) << 7) | (b[3] & 0x7F)
    return size + 10


def read_flac_streaminfo(path: Path) -> dict[str, int | bool] | None:
    try:
        with path.open("rb") as handle:
            prefix = handle.read(10)
            skip = _id3_size(prefix)
            handle.seek(skip)
            if handle.read(4) != b"fLaC":
                return None
            block_header = handle.read(4)
            if len(block_header) < 4:
                return None
            block_type = block_header[0] & 0x7F
            length = int.from_bytes(block_header[1:4], "big")
            if block_type != 0 or length < 34:
                return None
            streaminfo = handle.read(34)
            if len(streaminfo) < 34:
                return None
            min_bs, max_bs = struct.unpack(">HH", streaminfo[0:4])
            packed = int.from_bytes(streaminfo[10:18], "big")
            sample_rate = (packed >> 44) & 0xFFFFF
            channels = ((packed >> 41) & 0x7) + 1
            bits_per_sample = ((packed >> 36) & 0x1F) + 1
            total_samples = packed & 0xFFFFFFFFF
            return {
                "min_blocksize": min_bs,
                "max_blocksize": max_bs,
                "sample_rate": sample_rate,
                "channels": channels,
                "bits_per_sample": bits_per_sample,
                "total_samples": total_samples,
                "epsilon_ms": (total_samples * 1000 // sample_rate) if sample_rate else 0,
                "id3_prefix": skip > 0,
            }
    except OSError:
        return None


def probe(path: str | Path, ffprobe_path: str | None = None) -> AudioProbe:
    file_path = Path(path)
    result = AudioProbe(path=str(file_path), size_bytes=file_path.stat().st_size if file_path.exists() else 0)
    if not file_path.exists():
        result.error = "File does not exist"
        return result
    if result.size_bytes == 0:
        result.error = "File is empty"
        return result

    flac_info = read_flac_streaminfo(file_path) if file_path.suffix.lower() in {".flac", ".fla"} else None
    if flac_info:
        result.backend = "flac-streaminfo"
        result.flac_min_blocksize = int(flac_info["min_blocksize"])
        result.flac_max_blocksize = int(flac_info["max_blocksize"])
        result.has_id3_prefix = bool(flac_info["id3_prefix"])
        result.bit_depth = int(flac_info["bits_per_sample"])
        result.sample_rate = int(flac_info["sample_rate"])
        result.channels = int(flac_info["channels"])
        result.duration_ms = int(flac_info["epsilon_ms"]) or None
        result.codec = "flac"
        result.format_name = "flac"

    try:
        tool = require_ffprobe(ffprobe_path)
    except AudioToolMissing as exc:
        if not flac_info and _mutagen_probe(file_path, result):
            return result
        if not flac_info:
            result.error = str(exc)
        return result

    command = [
        tool,
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        str(file_path),
    ]
    try:
        completed = subprocess.run(command, capture_output=True, text=True, timeout=120, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        result.error = f"ffprobe failed: {exc}"
        return result
    if completed.returncode != 0:
        result.error = (completed.stderr or "ffprobe failed").strip()[:500]
        return result
    try:
        payload = json.loads(completed.stdout or "{}")
    except json.JSONDecodeError:
        result.error = "ffprobe returned invalid JSON"
        return result

    streams = payload.get("streams") or []
    audio_streams = [s for s in streams if s.get("codec_type") == "audio"]
    stream = audio_streams[0] if audio_streams else {}
    fmt = payload.get("format") or {}

    result.format_name = fmt.get("format_name") or result.format_name
    result.codec = stream.get("codec_name") or result.codec
    if stream.get("bits_per_raw_sample"):
        with contextlib.suppress(TypeError, ValueError):
            result.bit_depth = int(stream["bits_per_raw_sample"])
    if result.bit_depth is None and stream.get("bits_per_sample"):
        with contextlib.suppress(TypeError, ValueError):
            result.bit_depth = int(stream["bits_per_sample"])
    if stream.get("sample_rate"):
        result.sample_rate = int(stream["sample_rate"])
    if stream.get("channels"):
        result.channels = int(stream["channels"])
    if stream.get("bit_rate"):
        result.bitrate = int(stream["bit_rate"])
    elif fmt.get("bit_rate"):
        with contextlib.suppress(TypeError, ValueError):
            result.bitrate = int(fmt["bit_rate"])
    if fmt.get("duration"):
        with contextlib.suppress(TypeError, ValueError):
            result.duration_ms = int(float(fmt["duration"]) * 1000)
    elif stream.get("duration"):
        with contextlib.suppress(TypeError, ValueError):
            result.duration_ms = int(float(stream["duration"]) * 1000)
    tags = {str(k).lower(): str(v) for k, v in (fmt.get("tags") or {}).items()}
    tags.update({str(k).lower(): str(v) for k, v in (stream.get("tags") or {}).items()})
    result.tags = tags
    result.backend = "ffprobe"
    return result


def _mutagen_probe(file_path: Path, result: AudioProbe) -> bool:
    """Best-effort property reading when ffprobe is unavailable.

    Mutagen does not decode audio, so this is a fallback for container-level
    properties only. Validation still applies the device rules; callers are
    warned that FFmpeg gives a fuller check.
    """
    try:
        from mutagen import File as MutagenFile
    except ImportError:
        return False
    try:
        audio = MutagenFile(file_path)
    except Exception:
        return False
    info = getattr(audio, "info", None)
    if info is None:
        return False

    suffix = file_path.suffix.lower()
    module = type(audio).__module__.lower()
    sample_rate = getattr(info, "sample_rate", None)
    length = getattr(info, "length", None)
    if not sample_rate and not length:
        return False

    if suffix == ".mp3":
        codec = "mp3"
    elif suffix in {".m4a", ".mp4", ".aac"}:
        codec_value = str(getattr(info, "codec", "") or "").lower()
        codec = "alac" if "alac" in codec_value else "aac"
    elif suffix in {".ogg", ".oga", ".opus"}:
        codec = "opus" if "opus" in module or suffix == ".opus" else "vorbis"
    elif suffix == ".wav":
        codec = "wav"
    elif suffix in {".dsf", ".dff"}:
        codec = "dsd_lsbf"
    elif suffix == ".wma":
        codec = "wmav2"
    elif suffix == ".ape":
        codec = "ape"
    else:
        codec = suffix.lstrip(".") or "unknown"

    result.codec = codec
    result.format_name = suffix.lstrip(".") or None
    result.backend = "mutagen"
    if sample_rate:
        result.sample_rate = int(sample_rate)
    if getattr(info, "channels", None):
        result.channels = int(info.channels)
    if getattr(info, "bitrate", None):
        result.bitrate = int(info.bitrate)
    if getattr(info, "bits_per_sample", None):
        result.bit_depth = int(info.bits_per_sample)
    if codec.startswith("dsd"):
        result.bit_depth = 1
    if length:
        result.duration_ms = int(float(length) * 1000)
    return True
