from __future__ import annotations

import logging
import math
import random
import time
from dataclasses import dataclass

import numpy as np

from digital_finder.models import Coordinates, Frame, SolveMetrics, SolveResult, now_utc_iso, wrap_ra_deg
from digital_finder.services.interfaces import CameraClient, PlateSolver, TelescopeClient

logger = logging.getLogger(__name__)
SIMULATED_DEG_PER_PX = 0.01
SIMULATED_WIDTH_PX = 640
SIMULATED_HEIGHT_PX = 480


@dataclass
class HiddenOffset:
    ra_deg: float = 0.35
    dec_deg: float = -0.18


class SimulatedTelescopeClient(TelescopeClient):
    def __init__(self, epoch: str = "J2000") -> None:
        self._epoch = epoch
        self._connected = True
        self._coords = Coordinates(ra_deg=210.0, dec_deg=20.0, epoch=epoch)
        self._target: Coordinates | None = None
        self._slew_end_s: float = 0.0

    def is_connected(self) -> bool:
        return self._connected

    def slew_to_coordinates(self, target: Coordinates, timeout_s: float) -> None:
        if not self._connected:
            raise TimeoutError("Simulated telescope is disconnected")
        distance = abs(target.ra_deg - self._coords.ra_deg) + abs(target.dec_deg - self._coords.dec_deg)
        duration = max(2.0, min(12.0, distance / 20.0))
        if duration > timeout_s:
            raise TimeoutError("Slew timeout before command accepted")
        self._target = target.normalized()
        self._slew_end_s = time.monotonic() + duration
        logger.info("SIM slew requested target_ra=%.4f target_dec=%.4f duration=%.1fs", target.ra_deg, target.dec_deg, duration)

    def is_slewing(self) -> bool:
        if self._target is not None and time.monotonic() >= self._slew_end_s:
            self._coords = self._target
            self._target = None
            logger.info("SIM slew complete ra=%.4f dec=%.4f", self._coords.ra_deg, self._coords.dec_deg)
        return self._target is not None

    def get_coordinates(self, timeout_s: float) -> Coordinates:
        if timeout_s < 0.1:
            raise TimeoutError("Coordinate read timeout")
        self.is_slewing()
        return self._coords.normalized()

    def sync_to_coordinates(self, target: Coordinates, timeout_s: float) -> None:
        if timeout_s < 0.1:
            raise TimeoutError("Sync timeout")
        self._coords = target.normalized()
        logger.info("SIM sync ra=%.4f dec=%.4f", self._coords.ra_deg, self._coords.dec_deg)


class SimulatedCameraClient(CameraClient):
    def __init__(
        self,
        mount_provider,
        hidden_offset: HiddenOffset | None = None,
        epoch: str = "J2000",
        sample_frame_provider=None,
    ) -> None:
        self._connected = True
        self._exposure_ms = 1500
        self._gain = 120
        self._mount_provider = mount_provider
        self._hidden_offset = hidden_offset or HiddenOffset()
        self._epoch = epoch
        self._sample_frame_provider = sample_frame_provider

    def is_connected(self) -> bool:
        return self._connected

    def set_exposure_ms(self, exposure_ms: int) -> None:
        self._exposure_ms = max(50, int(exposure_ms))

    def set_gain(self, gain: int) -> None:
        self._gain = max(0, min(600, int(gain)))

    def capture_frame(self, timeout_s: float) -> Frame:
        capture_duration = max(0.05, self._exposure_ms / 1000.0)
        if capture_duration > timeout_s:
            raise TimeoutError("Camera capture timed out")

        if self._sample_frame_provider is not None:
            return self._sample_frame_provider()

        mount = self._mount_provider()
        finder_ra = wrap_ra_deg(mount.ra_deg - self._hidden_offset.ra_deg)
        finder_dec = max(-90.0, min(90.0, mount.dec_deg - self._hidden_offset.dec_deg))
        finder_coords = Coordinates(ra_deg=finder_ra, dec_deg=finder_dec, epoch=self._epoch)

        image = self._generate_starfield(finder_coords)
        return Frame(data=image, captured_at_utc=now_utc_iso(), source_path=None, true_coords=finder_coords)

    def _generate_starfield(self, coords: Coordinates) -> np.ndarray:
        size = (SIMULATED_HEIGHT_PX, SIMULATED_WIDTH_PX)
        seed = int(coords.ra_deg * 1000 + (coords.dec_deg + 90.0) * 1000)
        rng = np.random.default_rng(seed)
        image = rng.normal(loc=550, scale=80, size=size)

        num_stars = 120
        ys = rng.integers(0, size[0], size=num_stars)
        xs = rng.integers(0, size[1], size=num_stars)
        mags = rng.uniform(1000, 4000, size=num_stars)

        for x, y, mag in zip(xs, ys, mags):
            radius = 1 + int((mag / 4000) * 2)
            for dy in range(-radius, radius + 1):
                for dx in range(-radius, radius + 1):
                    xx = x + dx
                    yy = y + dy
                    if 0 <= xx < size[1] and 0 <= yy < size[0]:
                        falloff = math.exp(-0.7 * (dx * dx + dy * dy))
                        image[yy, xx] += mag * falloff

        image *= 1.0 + (self._gain / 800.0)
        image = np.clip(image, 0, 65535).astype(np.uint16)
        return image


class SimulatedPlateSolver(PlateSolver):
    def solve(self, frame: Frame, timeout_s: float) -> SolveResult:
        if timeout_s < 0.1:
            return SolveResult(success=False, message="Plate solve timeout")
        if frame.true_coords is None:
            return SolveResult(success=False, message="No truth coordinates in simulated frame")
        confidence = round(random.uniform(0.90, 0.99), 3)
        metrics = SolveMetrics(
            image_scale_arcsec_per_px=SIMULATED_DEG_PER_PX * 3600.0,
            rotation_deg=0.0,
            fov_width_deg=SIMULATED_WIDTH_PX * SIMULATED_DEG_PER_PX,
            fov_height_deg=SIMULATED_HEIGHT_PX * SIMULATED_DEG_PER_PX,
            cd1_1=-SIMULATED_DEG_PER_PX,
            cd1_2=0.0,
            cd2_1=0.0,
            cd2_2=SIMULATED_DEG_PER_PX,
        )
        logger.info(
            "SIM solve success ra=%.5f dec=%.5f confidence=%.3f",
            frame.true_coords.ra_deg,
            frame.true_coords.dec_deg,
            confidence,
        )
        return SolveResult(
            success=True,
            coordinates=frame.true_coords.normalized(),
            confidence=confidence,
            metrics=metrics,
            message="Simulated solve success",
        )
