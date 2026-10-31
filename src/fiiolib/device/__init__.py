"""Device package."""

from fiiolib.device.detect import (
    DeviceError,
    DeviceLocator,
    VolumeInfo,
    device_identifier,
    list_external_volumes,
)

__all__ = ["DeviceError", "DeviceLocator", "VolumeInfo", "device_identifier", "list_external_volumes"]
