from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any


def now_utc_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def wrap_ra_deg(value: float) -> float:
    return value % 360.0


def clamp_dec_deg(value: float) -> float:
    return max(-90.0, min(90.0, value))


@dataclass
class Coordinates:
    ra_deg: float
    dec_deg: float
    epoch: str = "J2000"

    def normalized(self) -> "Coordinates":
        return Coordinates(ra_deg=wrap_ra_deg(self.ra_deg), dec_deg=clamp_dec_deg(self.dec_deg), epoch=self.epoch)


@dataclass
class Frame:
    data: Any
    captured_at_utc: str
    source_path: str | None = None
    true_coords: Coordinates | None = None


@dataclass
class SolveResult:
    success: bool
    coordinates: Coordinates | None = None
    confidence: float | None = None
    message: str = ""


@dataclass
class CalibrationStar:
    name: str
    ra_deg: float
    dec_deg: float
    notes: str = ""


@dataclass
class CalibrationRecord:
    timestamp_utc: str
    epoch: str
    star_name: str
    star_ra_deg: float
    star_dec_deg: float
    mount_ra_deg: float
    mount_dec_deg: float
    finder_ra_deg: float
    finder_dec_deg: float
    offset_ra_deg: float
    offset_dec_deg: float
    solve_confidence: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "CalibrationRecord":
        return cls(**payload)
