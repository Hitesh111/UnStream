"""Artwork retrieval and preparation for the Echo Mini."""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)

CAA_RELEASE_GROUP = "https://coverartarchive.org/release-group/{mbid}/front-{size}"
CAA_RELEASE = "https://coverartarchive.org/release/{mbid}/front-{size}"


def is_progressive_jpeg(data: bytes) -> bool:
    if not data or data[:3] != b"\xff\xd8\xff":
        return False
    index = 2
    while index + 9 < len(data):
        if data[index] != 0xFF:
            index += 1
            continue
        marker = data[index + 1]
        if marker in (0xC0, 0xC1, 0xC2, 0xC3):
            return marker == 0xC2
        if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:
            index += 2
            continue
        length = int.from_bytes(data[index + 2 : index + 4], "big")
        index += 2 + length
    return False


def fetch_cover_art(
    release_group_mbid: str | None = None,
    release_mbid: str | None = None,
    size: int = 500,
    timeout: float = 30.0,
) -> bytes | None:
    url = None
    if release_group_mbid:
        url = CAA_RELEASE_GROUP.format(mbid=release_group_mbid, size=size)
    elif release_mbid:
        url = CAA_RELEASE.format(mbid=release_mbid, size=size)
    if not url:
        return None
    try:
        response = httpx.get(url, follow_redirects=True, timeout=timeout)
    except httpx.HTTPError as exc:
        logger.info("Cover Art Archive request failed: %s", exc)
        return None
    if response.status_code != 200:
        return None
    return response.content


def fetch_image(url: str, timeout: float = 30.0) -> bytes | None:
    if not url:
        return None
    try:
        response = httpx.get(url, follow_redirects=True, timeout=timeout)
    except httpx.HTTPError as exc:
        logger.info("Artwork download failed: %s", exc)
        return None
    return response.content if response.status_code == 200 else None


def prepare_artwork(
    data: bytes,
    target_px: int = 500,
    ffmpeg_path: str | None = None,
) -> tuple[bytes | None, list[str]]:
    """Return device-friendly baseline JPEG, converting only when required."""
    notes: list[str] = []
    if not data:
        return None, ["No artwork data"]
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        notes.append("PNG artwork converted to JPEG")
        return _convert_with_ffmpeg(data, target_px, ffmpeg_path, notes) or (None, notes)
    if data[:3] != b"\xff\xd8\xff":
        return None, ["Artwork is neither JPEG nor PNG"]
    if is_progressive_jpeg(data):
        notes.append("Progressive JPEG converted to baseline for device compatibility")
        converted = _convert_with_ffmpeg(data, target_px, ffmpeg_path, notes)
        if converted:
            return converted
        notes.append("Could not convert progressive JPEG; embedded as-is")
    return data, notes


def _convert_with_ffmpeg(
    data: bytes, target_px: int, ffmpeg_path: str | None, notes: list[str]
) -> tuple[bytes, list[str]] | None:
    import shutil

    tool = ffmpeg_path if ffmpeg_path and Path(ffmpeg_path).is_file() else shutil.which("ffmpeg")
    if not tool:
        notes.append("ffmpeg unavailable; cannot convert or resize artwork")
        return None
    command = [
        tool,
        "-v",
        "error",
        "-i",
        "pipe:0",
        "-vf",
        f"scale={target_px}:{target_px}:force_original_aspect_ratio=decrease",
        "-frames:v",
        "1",
        "-f",
        "image2",
        "-c:v",
        "mjpeg",
        "-q:v",
        "3",
        "pipe:1",
    ]
    try:
        completed = subprocess.run(
            command, input=data, capture_output=True, timeout=60, check=False
        )
    except (OSError, subprocess.SubprocessError) as exc:
        notes.append(f"Artwork conversion failed: {exc}")
        return None
    if completed.returncode != 0 or not completed.stdout:
        notes.append(f"Artwork conversion failed: {completed.stderr[:200]!r}")
        return None
    return completed.stdout, notes


def save_artwork(data: bytes, destination: Path) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(data)
    return destination
