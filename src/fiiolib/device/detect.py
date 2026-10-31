"""macOS volume detection for the microSD card (diskutil, no fixed mount path)."""

from __future__ import annotations

import logging
import plistlib
import subprocess
from dataclasses import dataclass, field
from typing import Any

from fiiolib.config import Config

logger = logging.getLogger(__name__)

IGNORED_FILESYSTEMS = {"apfs", "hfs", "hfs+", "ntfs"}
ECHO_MARKERS = ("echo mini", "echo", "snowsky")


class DeviceError(RuntimeError):
    pass


@dataclass
class VolumeInfo:
    identifier: str
    volume_name: str
    mount_point: str | None
    filesystem: str
    capacity_bytes: int
    device: str
    removable: bool = False
    internal: bool = True
    bus_protocol: str | None = None
    media_name: str | None = None
    volume_uuid: str | None = None
    is_echo_candidate: bool = False
    reasons: list[str] = field(default_factory=list)

    @property
    def capacity_gb(self) -> float:
        return round(self.capacity_bytes / 1024**3, 1)

    def as_dict(self) -> dict[str, Any]:
        return {
            "identifier": self.identifier,
            "volume_name": self.volume_name,
            "mount_point": self.mount_point,
            "filesystem": self.filesystem,
            "capacity_bytes": self.capacity_bytes,
            "capacity_gb": self.capacity_gb,
            "device": self.device,
            "removable": self.removable,
            "internal": self.internal,
            "bus_protocol": self.bus_protocol,
            "media_name": self.media_name,
            "volume_uuid": self.volume_uuid,
            "is_echo_candidate": self.is_echo_candidate,
            "reasons": self.reasons,
        }


def _run_plist(arguments: list[str]) -> dict[str, Any]:
    try:
        completed = subprocess.run(arguments, capture_output=True, timeout=30, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        raise DeviceError(f"diskutil failed: {exc}") from exc
    if completed.returncode != 0:
        return {}
    try:
        return plistlib.loads(completed.stdout)
    except plistlib.InvalidFileException:
        return {}


def list_external_volumes() -> list[VolumeInfo]:
    listing = _run_plist(["diskutil", "list", "-plist"])
    if not listing:
        return []
    volumes: list[VolumeInfo] = []
    for disk in listing.get("AllDisksAndPartitions") or []:
        for partition in disk.get("Partitions") or []:
            identifier = partition.get("DeviceIdentifier")
            if not identifier:
                continue
            info = _run_plist(["diskutil", "info", "-plist", f"/dev/{identifier}"])
            if not info:
                continue
            if info.get("Internal", True) and not info.get("RemovableMedia", False):
                continue
            if str(info.get("BusProtocol") or "").lower() == "disk image":
                continue
            filesystem = str(info.get("FileSystemPersonality") or info.get("FilesystemType") or "").strip()
            mount_point = info.get("MountPoint")
            if not mount_point:
                continue
            volumes.append(
                VolumeInfo(
                    identifier=str(info.get("VolumeUUID") or identifier),
                    volume_name=str(info.get("VolumeName") or ""),
                    mount_point=str(mount_point),
                    filesystem=filesystem.lower(),
                    capacity_bytes=int(info.get("TotalSize") or info.get("Size") or 0),
                    device=str(info.get("DeviceNode") or f"/dev/{identifier}"),
                    removable=bool(info.get("RemovableMedia") or False),
                    internal=bool(info.get("Internal", True)),
                    bus_protocol=info.get("BusProtocol"),
                    media_name=info.get("MediaName"),
                    volume_uuid=info.get("VolumeUUID"),
                )
            )
    return volumes


class DeviceLocator:
    def __init__(self, config: Config) -> None:
        self.config = config

    def candidates(self) -> list[VolumeInfo]:
        volumes = list_external_volumes()
        whitelist = {value.lower() for value in self.config.device.filesystem_whitelist}
        results: list[VolumeInfo] = []
        for volume in volumes:
            reasons: list[str] = []
            if volume.filesystem in whitelist:
                reasons.append(f"{volume.filesystem} is supported by the Echo Mini")
            else:
                reasons.append(f"{volume.filesystem or 'unknown'} is not a supported card filesystem")
            if volume.capacity_bytes >= 30 * 1024**3:
                reasons.append(f"{volume.capacity_gb:.1f} GB card")
            markers = " ".join(
                filter(None, [volume.volume_name, volume.media_name or ""])
            ).lower()
            if any(marker in markers for marker in ECHO_MARKERS):
                reasons.append("Name suggests an ECHO device")
            expected = self.config.device.expected_card_capacity_gb
            if abs(volume.capacity_gb - expected) <= max(2.0, expected * 0.1):
                reasons.append(f"Capacity close to the expected {expected} GB card")
            volume.reasons = reasons
            volume.is_echo_candidate = volume.filesystem in whitelist and volume.capacity_bytes >= 30 * 1024**3
            results.append(volume)
        return results

    def current(self, identifier: str | None = None) -> VolumeInfo:
        candidates = self.candidates()
        if identifier:
            for volume in candidates:
                if identifier in {volume.identifier, volume.volume_uuid, volume.device, volume.volume_name}:
                    return volume
            raise DeviceError(f"No mounted volume matches {identifier!r}")
        if not candidates:
            raise DeviceError("No external SD card volumes are mounted")
        if len(candidates) == 1:
            return candidates[0]
        echo = [v for v in candidates if v.is_echo_candidate]
        if len(echo) == 1:
            return echo[0]
        names = ", ".join(f"{v.volume_name} ({v.capacity_gb} GB)" for v in candidates)
        raise DeviceError(
            f"Several candidate volumes are mounted ({names}). Pass --device with a volume name, "
            "UUID, or device node."
        )


def device_identifier(volume: VolumeInfo) -> str:
    return volume.volume_uuid or volume.device or volume.volume_name
