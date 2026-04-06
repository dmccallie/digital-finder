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


def format_ra_hms(ra_deg: float) -> str:
    wrapped_deg = wrap_ra_deg(ra_deg)
    total_seconds = int(round((wrapped_deg / 15.0) * 3600.0)) % (24 * 3600)
    hours, rem = divmod(total_seconds, 3600)
    minutes, seconds = divmod(rem, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def format_dec_dms(dec_deg: float) -> str:
    sign = "-" if dec_deg < 0 else ""
    total_seconds = int(round(abs(dec_deg) * 3600.0))
    degrees, rem = divmod(total_seconds, 3600)
    minutes, seconds = divmod(rem, 60)
    return f"{sign}{degrees:03d}° {minutes:02d}' {seconds:02d}\""


def format_ra_deg_with_hms(ra_deg: float, precision: int = 5) -> str:
    return f"{ra_deg:.{precision}f}° ({format_ra_hms(ra_deg)})"


def format_dec_deg_with_dms(dec_deg: float, precision: int = 5) -> str:
    return f"{dec_deg:.{precision}f}° ({format_dec_dms(dec_deg)})"


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
    metrics: "SolveMetrics | None" = None
    message: str = ""


@dataclass
class SolveMetrics:
    image_scale_arcsec_per_px: float | None = None
    rotation_deg: float | None = None
    fov_width_deg: float | None = None
    fov_height_deg: float | None = None


def format_plate_solve_metrics(metrics: SolveMetrics | None) -> str:
    if metrics is None:
        return ""

    lines: list[str] = []
    if metrics.image_scale_arcsec_per_px is not None:
        lines.append(f"Image scale: {metrics.image_scale_arcsec_per_px:.3f}\"/pixel")
    if metrics.rotation_deg is not None:
        lines.append(f"Rotation (NCP): {metrics.rotation_deg:.2f}°")
    if metrics.fov_width_deg is not None and metrics.fov_height_deg is not None:
        lines.append(f"Field of view: {metrics.fov_width_deg:.3f}° x {metrics.fov_height_deg:.3f}°")

    return "\n".join(lines)


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
