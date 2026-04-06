from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class CameraDataType(str, Enum):
    MONO8 = "mono8"
    RAW16 = "raw16"
    RGB24 = "rgb24"


@dataclass
class CameraSettings:
    exposure_ms: int = 1500
    gain: int = 120
    binning: int = 2 # for my large camera, binning 2 is a good default for plate solving
    cooler_enabled: bool = False
    target_temperature_c: float | None = None
    data_type: CameraDataType = CameraDataType.RAW16


@dataclass
class ZwoCameraSettings(CameraSettings):
    camera_index: int = 0
