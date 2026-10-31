"""Audio probe, validation, and Echo Mini profile tests."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from fiiolib.audio.device_profile import (
    compatibility,
    device_filename,
    sanitize_component,
)
from fiiolib.audio.probe import AudioProbe, probe
from fiiolib.audio.validate import sha256_file, validate_audio

sf = pytest.importorskip("soundfile", reason="soundfile is needed to generate FLAC fixtures")


@pytest.fixture()
def flac_file(tmp_path: Path) -> Path:
    path = tmp_path / "sample.flac"
    samples = (0.2 * np.sin(2 * np.pi * 440 * np.linspace(0, 2, 2 * 44100))).astype("float32")
    sf.write(path, samples, 44100, format="FLAC", subtype="PCM_16")
    return path


def test_probe_flac_without_ffprobe(flac_file: Path) -> None:
    result = probe(flac_file)
    assert result.error is None
    assert result.codec == "flac"
    assert result.sample_rate == 44100
    assert result.bit_depth == 16
    assert result.channels == 1
    assert result.duration_ms is not None and 1800 <= result.duration_ms <= 2100
    assert result.flac_max_blocksize and result.flac_max_blocksize <= 4608
    assert result.backend == "flac-streaminfo"


def test_validate_flac_is_device_compatible(flac_file: Path) -> None:
    result = validate_audio(flac_file)
    assert result.ok, result.errors
    assert result.probe is not None
    assert result.sha256 == sha256_file(flac_file)
    assert result.transcode_suspect is False


@pytest.mark.parametrize(
    ("fmt", "subtype", "extension", "codec"),
    [("WAV", "PCM_16", "wav", "wav"), ("OGG", "VORBIS", "ogg", "vorbis")],
)
def test_mutagen_fallback_without_ffprobe(
    tmp_path: Path, fmt: str, subtype: str, extension: str, codec: str
) -> None:
    path = tmp_path / f"sample.{extension}"
    samples = (0.2 * np.sin(2 * np.pi * 330 * np.linspace(0, 2, 2 * 44100))).astype("float32")
    sf.write(path, samples, 44100, format=fmt, subtype=subtype)
    result = probe(path)
    assert result.error is None
    assert result.backend == "mutagen"
    assert result.codec == codec
    assert result.sample_rate == 44100
    assert result.duration_ms is not None and 1800 <= result.duration_ms <= 2100
    validation = validate_audio(path)
    assert validation.ok, validation.errors
    assert any("ffprobe is unavailable" in warning for warning in validation.warnings)


def test_compatibility_flags_unsupported_and_oversized() -> None:
    dsd = AudioProbe(path="x.dsf", size_bytes=10, codec="dsd_lsbf", sample_rate=2_822_400, bit_depth=1)
    assert compatibility(dsd).ok

    opus = AudioProbe(path="x.opus", size_bytes=10, codec="opus", sample_rate=48000, bit_depth=16, channels=2)
    verdict = compatibility(opus)
    assert not verdict.ok
    assert any("not supported" in error for error in verdict.errors)

    hires = AudioProbe(path="x.flac", size_bytes=10, codec="flac", sample_rate=384_000,
                       bit_depth=32, channels=2, flac_max_blocksize=8192)
    verdict = compatibility(hires)
    assert not verdict.ok
    assert len(verdict.errors) >= 3


def test_compound_track_numbers_and_filenames() -> None:
    assert device_filename("3/10", "A/B: Title?", ".FLAC") == "03 - AB Title.flac"
    assert device_filename(None, "Untitled", "flac") == "00 - Untitled.flac"
    assert sanitize_component("  ...  ") == "Unknown"
    assert len(sanitize_component("é" * 200)) <= 120
