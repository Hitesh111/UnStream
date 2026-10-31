"""Audio validation: integrity, codec, quality, transcode suspicion."""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fiiolib.audio.device_profile import (
    SUPPORTED_CODECS,
    SUPPORTED_EXTENSIONS,
    Compatibility,
    compatibility,
)
from fiiolib.audio.probe import AudioProbe, AudioToolMissing, probe

logger = logging.getLogger(__name__)

LOSSY_TO_LOSSLESS_WARNING_BITRATE = 500_000


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass
class ValidationResult:
    path: str
    ok: bool
    probe: AudioProbe | None
    findings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    compatibility: Compatibility | None = None
    sha256: str | None = None
    transcode_suspect: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "ok": self.ok,
            "findings": self.findings,
            "errors": self.errors,
            "warnings": self.warnings,
            "compatibility": self.compatibility.as_dict() if self.compatibility else None,
            "sha256": self.sha256,
            "transcode_suspect": self.transcode_suspect,
            "probe": self.probe.as_dict() if self.probe else None,
        }


def _transcode_heuristics(result: AudioProbe) -> tuple[bool, list[str]]:
    warnings: list[str] = []
    suspect = False
    codec = (result.codec or "").lower()
    if codec == "flac" and result.bitrate and result.bitrate < LOSSY_TO_LOSSLESS_WARNING_BITRATE:
        suspect = True
        warnings.append(
            f"FLAC bitrate {result.bitrate // 1000} kbps is unusually low; possible lossy-to-lossless transcode"
        )
    if codec in {"mp3", "aac"} and result.sample_rate and result.sample_rate > 48_000 and codec == "mp3":
        warnings.append("MP3 with a sample rate above 48 kHz is unusual and may be an upsampled transcode")
    if codec == "flac" and result.bit_depth and result.bit_depth > 16 and result.sample_rate in (44_100, 48_000):
        warnings.append("High-bit-depth FLAC at 44.1/48 kHz: verify the source is genuinely lossless")
    return suspect, warnings


def validate_audio(
    path: str | Path,
    ffprobe_path: str | None = None,
    compute_hash: bool = True,
    require_device_compat: bool = True,
) -> ValidationResult:
    file_path = Path(path)
    findings: list[str] = []
    errors: list[str] = []
    warnings: list[str] = []

    if not file_path.exists():
        return ValidationResult(str(file_path), False, None, errors=["File does not exist"])
    if file_path.stat().st_size < 1024:
        return ValidationResult(str(file_path), False, None, errors=["File is too small to be valid audio"])
    if file_path.suffix.lower() not in SUPPORTED_EXTENSIONS:
        findings.append(f"Extension {file_path.suffix} is not in the device-supported list")

    try:
        probe_result = probe(file_path, ffprobe_path=ffprobe_path)
    except AudioToolMissing as exc:
        return ValidationResult(str(file_path), False, None, errors=[str(exc)])

    if probe_result.error:
        return ValidationResult(str(file_path), False, probe_result, errors=[probe_result.error])
    if probe_result.backend == "mutagen":
        warnings.append(
            "Probed with mutagen because ffprobe is unavailable; install FFmpeg for a full verification"
        )

    codec = (probe_result.codec or "").lower()
    if codec and codec not in SUPPORTED_CODECS:
        errors.append(f"Codec {codec} is not supported by the Echo Mini")

    if not probe_result.duration_ms or probe_result.duration_ms < 1000:
        errors.append("Duration is missing or shorter than one second")
    if not probe_result.sample_rate:
        errors.append("Sample rate could not be determined")
    if not probe_result.channels:
        errors.append("Channel count could not be determined")

    if codec == "mp3" and probe_result.bitrate:
        if probe_result.bitrate > 330_000:
            warnings.append(f"MP3 bitrate {probe_result.bitrate // 1000} kbps exceeds 320 kbps")
        elif probe_result.bitrate < 64_000:
            errors.append(f"MP3 bitrate {probe_result.bitrate // 1000} kbps is too low for the collection")

    compat = compatibility(probe_result)
    if require_device_compat:
        errors.extend(compat.errors)
    warnings.extend(compat.warnings)

    suspect, transcode_warnings = _transcode_heuristics(probe_result)
    warnings.extend(transcode_warnings)

    findings.append(f"{codec or 'unknown'} / {probe_result.sample_rate or '?'} Hz / "
                    f"{probe_result.bit_depth or '?'} bit / {probe_result.channels or '?'} ch")

    digest = sha256_file(file_path) if compute_hash else None
    ok = not errors
    return ValidationResult(
        path=str(file_path),
        ok=ok,
        probe=probe_result,
        findings=findings,
        errors=errors,
        warnings=warnings,
        compatibility=compat,
        sha256=digest,
        transcode_suspect=suspect,
    )
