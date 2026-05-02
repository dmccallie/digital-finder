from __future__ import annotations

import atexit
import logging
from dataclasses import dataclass
from typing import Callable, TypeVar

import numpy as np
import pyzwoasi
from pyzwoasi import ZWOCamera
from pyzwoasi.pyzwoasi import ASIImageType

from digital_finder.models import Frame, now_utc_iso
from digital_finder.services.camera_settings import CameraDataType, CameraSettings, ZwoCameraSettings
from digital_finder.services.interfaces import CameraClient

logger = logging.getLogger(__name__)

T = TypeVar("T")


@dataclass(frozen=True)
class ZwoCameraInfo:
    camera_index: int
    name: str
    is_color: bool
    is_cooler: bool


def list_zwo_cameras() -> list[ZwoCameraInfo]:
    count = pyzwoasi.getNumOfConnectedCameras()
    cameras: list[ZwoCameraInfo] = []
    for idx in range(count):
        info = pyzwoasi.getCameraProperty(idx)
        cameras.append(
            ZwoCameraInfo(
                camera_index=idx,
                name=info.Name.decode("utf-8", errors="ignore"),
                is_color=bool(info.IsColorCam),
                is_cooler=bool(info.IsCoolerCam),
            )
        )
    return cameras


class ZwoCameraClient(CameraClient):
    def __init__(self, settings: ZwoCameraSettings | None = None) -> None:
        self._settings = settings or ZwoCameraSettings()
        self._camera: ZWOCamera | None = None
        self._connect()
        # Register cleanup so the SDK releases the camera on interpreter shutdown
        # (e.g. VS Code debug stop / Ctrl+C), not just on normal Qt close.
        atexit.register(self.close)

    def _connect(self) -> None:
        if self._camera is not None and not getattr(self._camera, "_isClosed", False):
            return
        if pyzwoasi.getNumOfConnectedCameras() <= self._settings.camera_index:
            raise RuntimeError("Requested ZWO camera index is not connected")
        # Defensively close before opening in case a previous connection attempt
        # (in this process) left the SDK handle in an inconsistent state.
        try:
            pyzwoasi.closeCamera(self._settings.camera_index)
        except Exception:  # noqa: BLE001
            pass  # expected when camera was not already open
        self._camera = ZWOCamera(self._settings.camera_index)

    def _reopen(self) -> None:
        self.close()
        self._connect()

    def _is_camera_closed_error(self, exc: Exception) -> bool:
        return "ASI_ERROR_CAMERA_CLOSED" in str(exc)

    def _run_with_reconnect(self, action: Callable[[ZWOCamera], T]) -> T:
        camera = self._require_camera()
        try:
            return action(camera)
        except Exception as exc:  # noqa: BLE001
            # Null out the handle immediately so no further calls reach the SDK
            # with a bad handle (avoids native crashes on USB disconnect).
            self.close()
            if not self._is_camera_closed_error(exc):
                # Not a clean SDK close — most likely a USB disconnect or
                # device removal. Raise a clean, catchable IOError.
                raise IOError(f"ZWO camera lost (USB disconnected?): {exc}") from exc
            # SDK reported ASI_ERROR_CAMERA_CLOSED — try once to reopen.
            try:
                self._connect()
            except Exception as reopen_exc:  # noqa: BLE001
                raise IOError(f"ZWO camera could not reopen: {reopen_exc}") from reopen_exc
            camera = self._require_camera()
            try:
                return action(camera)
            except Exception as retry_exc:  # noqa: BLE001
                self.close()
                raise IOError(f"ZWO camera lost after reopen: {retry_exc}") from retry_exc

    def close(self) -> None:
        if self._camera is not None:
            try:
                self._camera.close()
            except Exception:  # noqa: BLE001
                pass  # Ignore SDK errors on close (e.g. camera already gone after USB disconnect)
            self._camera = None

    def __del__(self) -> None:
        self.close()

    def is_connected(self) -> bool:
        return self._camera is not None and not getattr(self._camera, "_isClosed", False)

    def _require_camera(self) -> ZWOCamera:
        if not self.is_connected():
            self._connect()
        if self._camera is None:
            raise RuntimeError("ZWO camera is not connected")
        return self._camera

    def set_exposure_ms(self, exposure_ms: int) -> None:
        self._settings.exposure_ms = max(1, int(exposure_ms))
        self._run_with_reconnect(lambda camera: setattr(camera, "exposure", self._settings.exposure_ms * 1000))

    def set_gain(self, gain: int) -> None:
        self._settings.gain = max(0, int(gain))
        self._run_with_reconnect(lambda camera: setattr(camera, "gain", self._settings.gain))

    def set_binning(self, binning: int) -> None:
        self._settings.binning = max(1, int(binning))

        def _apply(camera: ZWOCamera) -> None:
            image_type = camera.imageType
            base_w = int(getattr(camera, "_maxWidth", camera.roi[0] * max(1, camera.roi[2])))
            base_h = int(getattr(camera, "_maxHeight", camera.roi[1] * max(1, camera.roi[2])))

            width = max(8, (base_w // self._settings.binning // 8) * 8)
            height = max(2, (base_h // self._settings.binning // 2) * 2)
            camera.setROI(width, height, binning=self._settings.binning, imageType=image_type)

        self._run_with_reconnect(_apply)

    def set_cooler_enabled(self, enabled: bool) -> None:
        self._settings.cooler_enabled = bool(enabled)

        def _apply(camera: ZWOCamera) -> None:
            if camera.cooler is not None:
                camera.cooler = self._settings.cooler_enabled

        self._run_with_reconnect(_apply)

    def set_target_temperature_c(self, temperature_c: float) -> None:
        self._settings.target_temperature_c = float(temperature_c)

        def _apply(camera: ZWOCamera) -> None:
            if hasattr(camera, "targetTemperature"):
                camera.targetTemperature(self._settings.target_temperature_c)

        self._run_with_reconnect(_apply)

    def set_data_type(self, data_type: CameraDataType) -> None:
        if data_type == CameraDataType.RGB24:
            raise ValueError("RGB24 mode is disabled in this app. Use RAW16 or MONO8.")
        self._settings.data_type = data_type

    def get_pixel_size_um(self) -> float | None:
        def _read(camera: ZWOCamera) -> float | None:
            try:
                info = pyzwoasi.getCameraProperty(self._settings.camera_index)
            except Exception:  # noqa: BLE001
                info = None

            value = getattr(info, "PixelSize", None) if info is not None else getattr(camera, "_pixelSize", None)
            if value is None:
                return None
            try:
                size = float(value)
            except (TypeError, ValueError):
                return None
            return size if size > 0 else None

        return self._run_with_reconnect(_read)

    def _is_color_camera(self) -> bool:
        try:
            info = pyzwoasi.getCameraProperty(self._settings.camera_index)
            return bool(info.IsColorCam)
        except Exception:  # noqa: BLE001
            # If camera capabilities are unavailable, treat as non-color for safety.
            return False

    def apply_settings(self, settings: CameraSettings) -> None:
        self.set_exposure_ms(settings.exposure_ms)
        self.set_gain(settings.gain)
        self.set_binning(settings.binning)
        self.set_cooler_enabled(settings.cooler_enabled)
        if settings.target_temperature_c is not None:
            self.set_target_temperature_c(settings.target_temperature_c)
        self.set_data_type(settings.data_type)

    def _image_type(self) -> ASIImageType:
        if self._settings.data_type == CameraDataType.MONO8:
            return ASIImageType.ASI_IMG_Y8
        if self._settings.data_type == CameraDataType.RGB24:
            return ASIImageType.ASI_IMG_RGB24
        return ASIImageType.ASI_IMG_RAW16

    def capture_frame(self, timeout_s: float) -> Frame:
        exposure_s = max(0.001, self._settings.exposure_ms / 1000.0)
        if exposure_s > timeout_s:
            raise TimeoutError("ZWO capture timeout: exposure longer than timeout")

        try:
            image = self._run_with_reconnect(
                lambda camera: camera.shot(exposureTime_us=self._settings.exposure_ms * 1000, imageType=self._image_type())
            )
        except SystemExit:
            # pyzwoasi.shot() calls exit() after 3 consecutive failed exposures.
            # Intercept here before SystemExit can escape into PySide6's slot
            # dispatcher, which would trigger a full Qt application shutdown.
            logger.error("ZWO camera exposure failed 3 times; treating as camera lost")
            self.close()
            raise IOError("ZWO camera exposure failed 3 times (USB disconnected?)")

        if not isinstance(image, np.ndarray):
            image = np.asarray(image)

        return Frame(data=image, captured_at_utc=now_utc_iso(), source_path=None, true_coords=None)
