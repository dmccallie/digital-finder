from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any

import numpy as np
from astronomy import Horizon, Observer, Refraction, Time
import logging

logger = logging.getLogger(__name__)

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


@dataclass(frozen=True)
class HorizontalCoordinates:
    altitude_deg: float
    azimuth_deg: float


def format_horizontal_deg(horizontal: HorizontalCoordinates, precision: int = 3) -> str:
    return f"Alt {horizontal.altitude_deg:.{precision}f}°, Az {horizontal.azimuth_deg:.{precision}f}°"


def radec_to_horizontal(
    ra_deg: float,
    dec_deg: float,
    observer_latitude_deg: float,
    observer_longitude_deg: float,
) -> HorizontalCoordinates:
    observer = Observer(latitude=observer_latitude_deg, longitude=observer_longitude_deg, height=0.0)
    now = Time.Now()
    horizontal = Horizon(
        now,
        observer,
        wrap_ra_deg(ra_deg) / 15.0,
        clamp_dec_deg(dec_deg),
        Refraction.Normal,
    )
    return HorizontalCoordinates(altitude_deg=horizontal.altitude, azimuth_deg=horizontal.azimuth)


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
    cd1_1: float | None = None
    cd1_2: float | None = None
    cd2_1: float | None = None
    cd2_2: float | None = None


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


@dataclass(frozen=True)
class PreviewWcsReference:
    coordinates: Coordinates
    metrics: SolveMetrics
    captured_at_utc: str


def signed_ra_delta_deg(value_deg: float, reference_deg: float) -> float:
    return ((value_deg - reference_deg + 180.0) % 360.0) - 180.0


def preview_wcs_reference_is_valid(
    reference: PreviewWcsReference | None,
    current_coordinates: Coordinates | None,
    *,
    max_age_s: float,
    max_ra_shift_deg: float,
    max_dec_shift_deg: float,
    now_utc: datetime | None = None,
) -> bool:
    if reference is None:
        return False

    if now_utc is None:
        now_utc = datetime.now(tz=timezone.utc)

    try:
        captured_at = datetime.fromisoformat(reference.captured_at_utc.replace("Z", "+00:00"))
    except ValueError:
        return False

    if captured_at.tzinfo is None:
        captured_at = captured_at.replace(tzinfo=timezone.utc)

    age_s = (now_utc - captured_at).total_seconds()
    if age_s < 0.0 or age_s > max_age_s:
        return False

    if current_coordinates is None:
        return True

    ra_shift_deg = abs(signed_ra_delta_deg(current_coordinates.ra_deg, reference.coordinates.ra_deg))
    dec_shift_deg = abs(current_coordinates.dec_deg - reference.coordinates.dec_deg)
    return ra_shift_deg <= max_ra_shift_deg and dec_shift_deg <= max_dec_shift_deg


def calibration_preview_source_offset_px(
    solve_coordinates: Coordinates | None,
    metrics: SolveMetrics | None,
    calibration: CalibrationRecord | None,
) -> tuple[float, float] | None:
    if solve_coordinates is None or metrics is None or calibration is None:
        return None

    if (
        metrics.cd1_1 is None
        or metrics.cd1_2 is None
        or metrics.cd2_1 is None
        or metrics.cd2_2 is None
    ):
        return None

    determinant = (metrics.cd1_1 * metrics.cd2_2) - (metrics.cd1_2 * metrics.cd2_1)
    if math.isclose(determinant, 0.0, abs_tol=1e-12):
        return None

    ra_projected_deg = calibration.offset_ra_deg * math.cos(math.radians(clamp_dec_deg(solve_coordinates.dec_deg)))
    dec_deg = calibration.offset_dec_deg
    dx_px = ((metrics.cd2_2 * ra_projected_deg) - (metrics.cd1_2 * dec_deg)) / determinant
    dy_px = ((-metrics.cd2_1 * ra_projected_deg) + (metrics.cd1_1 * dec_deg)) / determinant
    return dx_px, dy_px


def shift_preview_image(
    image: np.ndarray,
    source_offset_x_px: float,
    source_offset_y_px: float,
    fill_value: int = 128,
) -> np.ndarray:
    # log to info the requested shift and fill value
    logger.info(f"Requested shift image by ({source_offset_x_px:.2f}, {source_offset_y_px:.2f}) pixels with fill value {fill_value}")
    if image.ndim != 2 or image.size == 0:
        return image

    shift_x_px = int(round(source_offset_x_px))
    shift_y_px = int(round(source_offset_y_px))
    if shift_x_px == 0 and shift_y_px == 0:
        return image

    shifted = np.full(image.shape, fill_value, dtype=image.dtype)
    height, width = image.shape

    src_x0 = max(0, shift_x_px)
    src_x1 = min(width, width + shift_x_px)
    src_y0 = max(0, shift_y_px)
    src_y1 = min(height, height + shift_y_px)
    if src_x1 <= src_x0 or src_y1 <= src_y0:
        return shifted

    dst_x0 = max(0, -shift_x_px)
    dst_y0 = max(0, -shift_y_px)
    dst_x1 = dst_x0 + (src_x1 - src_x0)
    dst_y1 = dst_y0 + (src_y1 - src_y0)
    shifted[dst_y0:dst_y1, dst_x0:dst_x1] = image[src_y0:src_y1, src_x0:src_x1]

    return shifted
