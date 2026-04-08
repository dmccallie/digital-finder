from __future__ import annotations

import json
import logging
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

import numpy as np
from astropy.io import fits
from platformdirs import user_data_dir
from PySide6 import QtCore, QtGui, QtWidgets

from digital_finder.config import (
    ALPACA_DISCOVERY_NUMQUERY,
    ALPACA_DISCOVERY_TIMEOUT_S,
    APP_NAME,
    APP_AUTHOR,
    MAIN_SCOPE_NAME,
    DEFAULT_ALPACA_DEVICE_NUMBER,
    DEFAULT_ALPACA_HOST,
    DEFAULT_ALPACA_PORT,
    DEFAULT_EPOCH,
    KNOWN_ALPACA_TELESCOPES,
    SOLVER_CONFIG,
    TIMEOUTS,
)
from digital_finder.logging_setup import configure_logging
from digital_finder.models import (
    Coordinates,
    Frame,
    SolveResult,
    clamp_dec_deg,
    format_dec_deg_with_dms,
    format_plate_solve_metrics,
    format_ra_deg_with_hms,
    now_utc_iso,
    wrap_ra_deg,
)
from digital_finder.services.alpaca_telescope import AlpacaTelescopeClient, DiscoveredTelescope, discover_alpaca_telescopes
from digital_finder.services.astap_solver import AstapPlateSolver
from digital_finder.services.camera_settings import CameraDataType, ZwoCameraSettings
from digital_finder.services.interfaces import CameraClient, PlateSolver, TelescopeClient
from digital_finder.services.simulated import SimulatedCameraClient
from digital_finder.services.zwo_camera import ZwoCameraClient
from digital_finder.storage import CalibrationStore
from digital_finder.ui.alignment_wizard import AlignmentWizardDialog

logger = logging.getLogger(__name__)
USER_TIMEZONE = ZoneInfo("America/Chicago")


@dataclass
class PersistentSettings:
    telescope_selected: str | None = None
    telescope_history: list[str] | None = None
    camera_selected: str = "zwo"
    zwo_camera_index: int = 0
    zwo_camera_name: str = ""
    camera_exposure_ms: int = 1500
    camera_gain: int = 120
    camera_binning: int = 1
    camera_data_type: str = CameraDataType.RAW16.value
    camera_looping: bool = True
    app_epoch: str = DEFAULT_EPOCH
    astap_executable: str = SOLVER_CONFIG.astap_executable
    astap_downsize_factor: int = 1
    reconnect_interval_s: int = 5
    logging_level: str = "INFO"
    image_stretch_level: int = 50
    app_window_width: int = 1100
    app_window_height: int = 720

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "PersistentSettings":
        data = dict(payload)
        return cls(
            telescope_selected=data.get("telescope_selected"),
            telescope_history=list(data.get("telescope_history", [])),
            camera_selected=str(data.get("camera_selected", "zwo")),
            zwo_camera_index=max(0, int(data.get("zwo_camera_index", 0))),
            zwo_camera_name=str(data.get("zwo_camera_name", "")),
            camera_exposure_ms=int(data.get("camera_exposure_ms", 1500)),
            camera_gain=int(data.get("camera_gain", 120)),
            camera_binning=int(data.get("camera_binning", 1)),
            camera_data_type=str(data.get("camera_data_type", CameraDataType.RAW16.value)),
            camera_looping=bool(data.get("camera_looping", True)),
            app_epoch=str(data.get("app_epoch", DEFAULT_EPOCH)),
            astap_executable=str(data.get("astap_executable", SOLVER_CONFIG.astap_executable)),
            astap_downsize_factor=max(1, int(data.get("astap_downsize_factor", 1))),
            reconnect_interval_s=max(5, int(data.get("reconnect_interval_s", 5))),
            logging_level=str(data.get("logging_level", "INFO")).upper(),
            image_stretch_level=min(100, max(0, int(data.get("image_stretch_level", 50)))),
            app_window_width=max(800, int(data.get("app_window_width", 1100))),
            app_window_height=max(600, int(data.get("app_window_height", 720))),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "telescope_selected": self.telescope_selected,
            "telescope_history": list(self.telescope_history or []),
            "camera_selected": self.camera_selected,
            "zwo_camera_index": self.zwo_camera_index,
            "zwo_camera_name": self.zwo_camera_name,
            "camera_exposure_ms": self.camera_exposure_ms,
            "camera_gain": self.camera_gain,
            "camera_binning": self.camera_binning,
            "camera_data_type": self.camera_data_type,
            "camera_looping": self.camera_looping,
            "app_epoch": self.app_epoch,
            "astap_executable": self.astap_executable,
            "astap_downsize_factor": self.astap_downsize_factor,
            "reconnect_interval_s": self.reconnect_interval_s,
            "logging_level": self.logging_level,
            "image_stretch_level": self.image_stretch_level,
            "app_window_width": self.app_window_width,
            "app_window_height": self.app_window_height,
        }


class _CaptureWorker(QtCore.QObject):
    frame_ready = QtCore.Signal(object)
    error = QtCore.Signal(str)
    finished = QtCore.Signal()

    def __init__(self, capture_fn: Callable[[], Frame]) -> None:
        super().__init__()
        self._capture_fn = capture_fn

    @QtCore.Slot()
    def run(self) -> None:
        try:
            frame = self._capture_fn()
            self.frame_ready.emit(frame)
        except Exception as exc:  # noqa: BLE001
            self.error.emit(str(exc))
        finally:
            self.finished.emit()


class _TelescopeConnectWorker(QtCore.QObject):
    connected = QtCore.Signal(object, str)
    failed = QtCore.Signal(str, str)
    finished = QtCore.Signal()

    def __init__(
        self,
        endpoint_text: str,
        host: str,
        port: int,
        device: int,
        epoch: str,
        timeout_s: float,
    ) -> None:
        super().__init__()
        self._endpoint_text = endpoint_text
        self._host = host
        self._port = port
        self._device = device
        self._epoch = epoch
        self._timeout_s = timeout_s

    @QtCore.Slot()
    def run(self) -> None:
        try:
            scope = AlpacaTelescopeClient(
                host=self._host,
                port=self._port,
                device_number=self._device,
                epoch=self._epoch,
                connect_timeout_s=self._timeout_s,
            )
            scope.connect(timeout_s=self._timeout_s)
            self.connected.emit(scope, self._endpoint_text)
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(str(exc), self._endpoint_text)
        finally:
            self.finished.emit()


class _ZoomableImageView(QtWidgets.QGraphicsView):
    def __init__(self, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self._scene = QtWidgets.QGraphicsScene(self)
        self.setScene(self._scene)
        self._pixmap_item = QtWidgets.QGraphicsPixmapItem()
        self._pixmap_item.setTransformationMode(QtCore.Qt.TransformationMode.SmoothTransformation)
        self._scene.addItem(self._pixmap_item)
        self._text_item: QtWidgets.QGraphicsSimpleTextItem | None = None
        self._has_image = False
        self._user_interacted = False
        self._min_zoom = 0.2
        self._max_zoom = 15.0

        self.setBackgroundBrush(QtGui.QBrush(QtGui.QColor("#111")))
        self.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        self.setRenderHints(QtGui.QPainter.RenderHint.Antialiasing | QtGui.QPainter.RenderHint.SmoothPixmapTransform)
        self.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.setVerticalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.setTransformationAnchor(QtWidgets.QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setResizeAnchor(QtWidgets.QGraphicsView.ViewportAnchor.AnchorViewCenter)
        self.setDragMode(QtWidgets.QGraphicsView.DragMode.NoDrag)

    def set_placeholder(self, text: str) -> None:
        self._has_image = False
        self._user_interacted = False
        self.resetTransform()
        self._pixmap_item.setPixmap(QtGui.QPixmap())
        self._scene.setSceneRect(QtCore.QRectF())
        if self._text_item is not None:
            self._scene.removeItem(self._text_item)
            self._text_item = None

        self._text_item = self._scene.addSimpleText(text)
        self._text_item.setBrush(QtGui.QBrush(QtGui.QColor("#eee")))
        self._position_placeholder_text()

    def set_pixmap(self, pixmap: QtGui.QPixmap) -> None:
        if self._text_item is not None:
            self._scene.removeItem(self._text_item)
            self._text_item = None

        self._pixmap_item.setPixmap(pixmap)
        self._scene.setSceneRect(self._pixmap_item.boundingRect())
        first_image = not self._has_image
        self._has_image = True
        if first_image or not self._user_interacted:
            self.reset_view()

    def reset_view(self) -> None:
        if not self._has_image:
            return
        rect = self._pixmap_item.boundingRect()
        if rect.isEmpty():
            return
        self._user_interacted = False
        self.resetTransform()
        self.fitInView(rect, QtCore.Qt.AspectRatioMode.KeepAspectRatio)

    def wheelEvent(self, event: QtGui.QWheelEvent) -> None:
        if not self._has_image:
            super().wheelEvent(event)
            return

        delta_y = event.angleDelta().y()
        if delta_y == 0:
            event.accept()
            return

        factor = 1.15 if delta_y > 0 else (1.0 / 1.15)
        current_scale = self.transform().m11()
        target_scale = current_scale * factor
        if target_scale < self._min_zoom:
            factor = self._min_zoom / current_scale
        elif target_scale > self._max_zoom:
            factor = self._max_zoom / current_scale

        self._user_interacted = True
        self.scale(factor, factor)
        event.accept()

    def mousePressEvent(self, event: QtGui.QMouseEvent) -> None:
        if self._has_image and event.button() == QtCore.Qt.MouseButton.LeftButton:
            self._user_interacted = True
            self.setDragMode(QtWidgets.QGraphicsView.DragMode.ScrollHandDrag)
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event: QtGui.QMouseEvent) -> None:
        super().mouseReleaseEvent(event)
        if event.button() == QtCore.Qt.MouseButton.LeftButton:
            self.setDragMode(QtWidgets.QGraphicsView.DragMode.NoDrag)

    def mouseDoubleClickEvent(self, event: QtGui.QMouseEvent) -> None:
        if self._has_image and event.button() == QtCore.Qt.MouseButton.LeftButton:
            self.reset_view()
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    def resizeEvent(self, event: QtGui.QResizeEvent) -> None:
        super().resizeEvent(event)
        if self._has_image and not self._user_interacted:
            self.reset_view()
        if self._text_item is not None:
            self._position_placeholder_text()

    def _position_placeholder_text(self) -> None:
        if self._text_item is None:
            return
        vp_rect = self.viewport().rect()
        scene_center = self.mapToScene(vp_rect.center())
        text_rect = self._text_item.boundingRect()
        self._text_item.setPos(scene_center.x() - text_rect.width() / 2, scene_center.y() - text_rect.height() / 2)


class TelescopeSettingsDialog(QtWidgets.QDialog):
    def __init__(self, history: list[str], selected: str | None, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Telescope Settings")
        self.resize(460, 260)

        self._combo = QtWidgets.QComboBox()
        self._combo.addItem("No Telescope", None)
        for entry in history:
            self._combo.addItem(entry, entry)

        self._host = QtWidgets.QLineEdit(DEFAULT_ALPACA_HOST)
        self._port = QtWidgets.QSpinBox()
        self._port.setRange(1, 65535)
        self._port.setValue(DEFAULT_ALPACA_PORT)
        self._device = QtWidgets.QSpinBox()
        self._device.setRange(0, 32)
        self._device.setValue(DEFAULT_ALPACA_DEVICE_NUMBER)

        self._use_entered = QtWidgets.QPushButton("Use Entered Endpoint")
        self._save = QtWidgets.QPushButton("Save")
        self._cancel = QtWidgets.QPushButton("Cancel")

        grid = QtWidgets.QGridLayout(self)
        grid.addWidget(QtWidgets.QLabel("Remembered endpoint"), 0, 0)
        grid.addWidget(self._combo, 0, 1, 1, 2)
        grid.addWidget(QtWidgets.QLabel("Host"), 1, 0)
        grid.addWidget(self._host, 1, 1, 1, 2)
        grid.addWidget(QtWidgets.QLabel("Port"), 2, 0)
        grid.addWidget(self._port, 2, 1, 1, 2)
        grid.addWidget(QtWidgets.QLabel("Device #"), 3, 0)
        grid.addWidget(self._device, 3, 1, 1, 2)
        grid.addWidget(self._use_entered, 4, 0, 1, 3)

        row = QtWidgets.QHBoxLayout()
        row.addStretch(1)
        row.addWidget(self._cancel)
        row.addWidget(self._save)
        grid.addLayout(row, 5, 0, 1, 3)

        self._combo.currentIndexChanged.connect(self._on_pick_changed)
        self._use_entered.clicked.connect(self._on_use_entered)
        self._save.clicked.connect(self.accept)
        self._cancel.clicked.connect(self.reject)

        if selected is None:
            self._combo.setCurrentIndex(0)
        else:
            idx = self._combo.findData(selected)
            if idx >= 0:
                self._combo.setCurrentIndex(idx)
            else:
                self._combo.addItem(selected, selected)
                self._combo.setCurrentIndex(self._combo.count() - 1)

        self._on_pick_changed()

    def _parse_endpoint(self, text: str) -> tuple[str, int, int] | None:
        parts = text.split(":")
        if len(parts) < 2:
            return None
        host = parts[0].strip()
        if not host:
            return None
        try:
            port = int(parts[1])
            device = int(parts[2]) if len(parts) > 2 else DEFAULT_ALPACA_DEVICE_NUMBER
        except ValueError:
            return None
        return (host, port, device)

    def _build_endpoint_text(self) -> str:
        return f"{self._host.text().strip() or DEFAULT_ALPACA_HOST}:{self._port.value()}:{self._device.value()}"

    def _on_pick_changed(self) -> None:
        selected = self._combo.currentData()
        if not isinstance(selected, str):
            return
        parsed = self._parse_endpoint(selected)
        if parsed is None:
            return
        host, port, device = parsed
        self._host.setText(host)
        self._port.setValue(port)
        self._device.setValue(device)

    def _on_use_entered(self) -> None:
        endpoint = self._build_endpoint_text()
        idx = self._combo.findData(endpoint)
        if idx < 0:
            self._combo.addItem(endpoint, endpoint)
            idx = self._combo.count() - 1
        self._combo.setCurrentIndex(idx)

    @property
    def selected_endpoint(self) -> str | None:
        current = self._combo.currentData()
        if current is None:
            return None
        if not isinstance(current, str):
            return None
        return current

    @property
    def history(self) -> list[str]:
        entries: list[str] = []
        for idx in range(self._combo.count()):
            data = self._combo.itemData(idx)
            if isinstance(data, str):
                entries.append(data)
        return entries


class CameraSettingsDialog(QtWidgets.QDialog):
    def __init__(self, settings: PersistentSettings, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Camera Settings")
        self.resize(460, 340)

        self._camera_combo = QtWidgets.QComboBox()
        self._camera_combo.addItem("No Camera", "none")
        self._camera_combo.addItem("ZWO Camera", "zwo")
        self._camera_combo.addItem("Simulator Camera", "simulator")

        self._zwo_combo = QtWidgets.QComboBox()
        self._initial_zwo_index = settings.zwo_camera_index
        if settings.zwo_camera_name:
            self._zwo_combo.addItem(
                f"{settings.zwo_camera_name} (index {settings.zwo_camera_index})",
                (settings.zwo_camera_index, settings.zwo_camera_name, None),
            )
        else:
            self._zwo_combo.addItem("Not scanned yet", (settings.zwo_camera_index, "", None))
        self._scan_btn = QtWidgets.QPushButton("Scan for Cameras")

        self._exp = QtWidgets.QSpinBox()
        self._exp.setRange(50, 30_000)
        self._exp.setSingleStep(250)
        self._exp.setSuffix(" ms")
        self._exp.setValue(settings.camera_exposure_ms)

        self._gain = QtWidgets.QSpinBox()
        self._gain.setRange(0, 600)
        self._gain.setSingleStep(50)
        self._gain.setValue(settings.camera_gain)

        self._binning = QtWidgets.QSpinBox()
        self._binning.setRange(1, 4)
        self._binning.setValue(settings.camera_binning)

        self._mode = QtWidgets.QComboBox()
        self._set_mode_options(is_color_camera=None, preferred=settings.camera_data_type)

        self._looping = QtWidgets.QCheckBox("Start continuous loop on launch")
        self._looping.setChecked(settings.camera_looping)

        idx = self._camera_combo.findData(settings.camera_selected)
        self._camera_combo.setCurrentIndex(idx if idx >= 0 else 1)

        save = QtWidgets.QPushButton("Save")
        cancel = QtWidgets.QPushButton("Cancel")

        grid = QtWidgets.QGridLayout(self)
        grid.addWidget(QtWidgets.QLabel("Camera"), 0, 0)
        grid.addWidget(self._camera_combo, 0, 1)
        grid.addWidget(QtWidgets.QLabel("ZWO device"), 1, 0)
        grid.addWidget(self._zwo_combo, 1, 1)
        grid.addWidget(self._scan_btn, 2, 0, 1, 2)
        grid.addWidget(QtWidgets.QLabel("Exposure"), 3, 0)
        grid.addWidget(self._exp, 3, 1)
        grid.addWidget(QtWidgets.QLabel("Gain"), 4, 0)
        grid.addWidget(self._gain, 4, 1)
        grid.addWidget(QtWidgets.QLabel("Binning"), 5, 0)
        grid.addWidget(self._binning, 5, 1)
        grid.addWidget(QtWidgets.QLabel("Mode"), 6, 0)
        grid.addWidget(self._mode, 6, 1)
        grid.addWidget(self._looping, 7, 0, 1, 2)

        row = QtWidgets.QHBoxLayout()
        row.addStretch(1)
        row.addWidget(cancel)
        row.addWidget(save)
        grid.addLayout(row, 8, 0, 1, 2)

        self._camera_combo.currentIndexChanged.connect(self._update_zwo_controls)
        self._zwo_combo.currentIndexChanged.connect(self._on_zwo_camera_changed)
        self._scan_btn.clicked.connect(self._scan_zwo_cameras)
        save.clicked.connect(self.accept)
        cancel.clicked.connect(self.reject)

        self._update_zwo_controls()

    def _set_mode_options(self, is_color_camera: bool | None, preferred: str | None = None) -> None:
        current = preferred if preferred is not None else str(self._mode.currentData() or CameraDataType.RAW16.value)
        self._mode.clear()
        self._mode.addItem("RAW16", CameraDataType.RAW16.value)
        self._mode.addItem("MONO8", CameraDataType.MONO8.value)

        idx = self._mode.findData(current)
        self._mode.setCurrentIndex(idx if idx >= 0 else 0)

    def _update_zwo_controls(self) -> None:
        is_zwo = self._camera_combo.currentData() == "zwo"
        self._zwo_combo.setEnabled(is_zwo)
        self._scan_btn.setEnabled(is_zwo)
        if not is_zwo:
            self._set_mode_options(is_color_camera=None)
        else:
            self._on_zwo_camera_changed()

    def _on_zwo_camera_changed(self) -> None:
        zwo_data = self._zwo_combo.currentData()
        is_color: bool | None = None
        if isinstance(zwo_data, tuple) and len(zwo_data) >= 3:
            raw = zwo_data[2]
            if isinstance(raw, bool):
                is_color = raw
        self._set_mode_options(is_color_camera=is_color)

    def _scan_zwo_cameras(self) -> None:
        from digital_finder.services.zwo_camera import list_zwo_cameras

        try:
            cameras = list_zwo_cameras()
        except Exception as exc:  # noqa: BLE001
            QtWidgets.QMessageBox.warning(self, "Camera Scan Failed", str(exc))
            return

        self._zwo_combo.clear()
        if not cameras:
            self._zwo_combo.addItem("No ZWO cameras detected", (-1, "", None))
            self._set_mode_options(is_color_camera=None)
            return

        selected_index = 0
        for camera in cameras:
            self._zwo_combo.addItem(camera.name, (camera.camera_index, camera.name, camera.is_color))
            if camera.camera_index == self._initial_zwo_index:
                selected_index = self._zwo_combo.count() - 1

        self._zwo_combo.setCurrentIndex(selected_index)
        self._on_zwo_camera_changed()

    def apply_to(self, settings: PersistentSettings) -> None:
        settings.camera_selected = str(self._camera_combo.currentData())
        zwo_data = self._zwo_combo.currentData()
        if isinstance(zwo_data, tuple) and len(zwo_data) >= 2:
            try:
                settings.zwo_camera_index = max(0, int(zwo_data[0]))
            except (TypeError, ValueError):
                settings.zwo_camera_index = 0
            settings.zwo_camera_name = str(zwo_data[1])
        settings.camera_exposure_ms = int(self._exp.value())
        settings.camera_gain = int(self._gain.value())
        settings.camera_binning = int(self._binning.value())
        settings.camera_data_type = str(self._mode.currentData())
        settings.camera_looping = self._looping.isChecked()


class AppSettingsDialog(QtWidgets.QDialog):
    def __init__(self, settings: PersistentSettings, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("App Settings")
        self.resize(520, 220)

        self._epoch = QtWidgets.QComboBox()
        self._epoch.addItems(["J2000", "JNOW"])
        idx = self._epoch.findText(settings.app_epoch)
        self._epoch.setCurrentIndex(idx if idx >= 0 else 0)

        self._astap = QtWidgets.QLineEdit(settings.astap_executable)
        self._astap_downsize = QtWidgets.QSpinBox()
        self._astap_downsize.setRange(1, 8)
        self._astap_downsize.setValue(max(1, settings.astap_downsize_factor))
        self._reconnect = QtWidgets.QSpinBox()
        self._reconnect.setRange(5, 30)
        self._reconnect.setSuffix(" s")
        self._reconnect.setValue(settings.reconnect_interval_s)

        self._logging_level = QtWidgets.QComboBox()
        self._logging_level.addItems(["DEBUG", "INFO", "WARNING", "ERROR"])
        level_idx = self._logging_level.findText(settings.logging_level.upper())
        self._logging_level.setCurrentIndex(level_idx if level_idx >= 0 else 1)

        save = QtWidgets.QPushButton("Save")
        cancel = QtWidgets.QPushButton("Cancel")

        grid = QtWidgets.QGridLayout(self)
        grid.addWidget(QtWidgets.QLabel("Epoch"), 0, 0)
        grid.addWidget(self._epoch, 0, 1)
        grid.addWidget(QtWidgets.QLabel("ASTAP executable"), 1, 0)
        grid.addWidget(self._astap, 1, 1)
        grid.addWidget(QtWidgets.QLabel("ASTAP downsize factor"), 2, 0)
        grid.addWidget(self._astap_downsize, 2, 1)
        grid.addWidget(QtWidgets.QLabel("Reconnect interval"), 3, 0)
        grid.addWidget(self._reconnect, 3, 1)
        grid.addWidget(QtWidgets.QLabel("Logging level"), 4, 0)
        grid.addWidget(self._logging_level, 4, 1)

        row = QtWidgets.QHBoxLayout()
        row.addStretch(1)
        row.addWidget(cancel)
        row.addWidget(save)
        grid.addLayout(row, 5, 0, 1, 2)

        save.clicked.connect(self.accept)
        cancel.clicked.connect(self.reject)

    def apply_to(self, settings: PersistentSettings) -> None:
        settings.app_epoch = self._epoch.currentText()
        settings.astap_executable = self._astap.text().strip() or SOLVER_CONFIG.astap_executable
        settings.astap_downsize_factor = max(1, int(self._astap_downsize.value()))
        settings.reconnect_interval_s = int(self._reconnect.value())
        settings.logging_level = self._logging_level.currentText()


class MainWindow(QtWidgets.QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(APP_NAME)

        self._settings_path = Path(user_data_dir(APP_NAME, APP_AUTHOR)) / "settings.json"
        print(f"Digital Finder Settings will be loaded from and saved to {self._settings_path}")
        self._settings = self._load_settings()
        self._epoch = self._settings.app_epoch
        self._apply_logging_level(self._settings.logging_level)
        self._apply_initial_window_size()

        self._store = CalibrationStore()
        self._latest_calibration = self._store.load_latest()

        self._telescope: TelescopeClient | None = None
        self._camera: CameraClient | None = None
        self._solver: PlateSolver = AstapPlateSolver(
            astap_executable=self._settings.astap_executable,
            downsample_factor=self._settings.astap_downsize_factor,
            approximate_coords_provider=self._astap_hint_coordinates,
        )

        self._latest_frame: Frame | None = None
        self._latest_solve: SolveResult | None = None

        self._telescope_last_error: str | None = None
        self._camera_last_error: str | None = None

        self._capture_thread: QtCore.QThread | None = None
        self._capture_worker: _CaptureWorker | None = None
        self._capture_in_progress = False
        self._telescope_connect_thread: QtCore.QThread | None = None
        self._telescope_connect_worker: _TelescopeConnectWorker | None = None
        self._telescope_connect_in_progress = False

        self._sample_image_path = self._resolve_sample_image_path(SOLVER_CONFIG.astap_test_image)

        self._live_timer = QtCore.QTimer(self)
        self._live_timer.setInterval(2000)
        self._live_timer.timeout.connect(self._capture_latest_frame)

        self._status_timer = QtCore.QTimer(self)
        self._status_timer.setInterval(1000)
        self._status_timer.timeout.connect(self._refresh_status_lines)

        self._telescope_retry_timer = QtCore.QTimer(self)
        self._telescope_retry_timer.timeout.connect(self._attempt_telescope_connect)

        self._camera_retry_timer = QtCore.QTimer(self)
        self._camera_retry_timer.timeout.connect(self._attempt_camera_connect)

        self._build_ui()
        self._status_timer.start()
        self._apply_retry_interval()

        # Auto-start behavior from saved config.
        self._attempt_telescope_connect()
        self._attempt_camera_connect()
        if self._settings.camera_looping:
            self._live_timer.start()

        self._refresh_status_lines()

    def _build_ui(self) -> None:
        central = QtWidgets.QWidget(self)
        root = QtWidgets.QVBoxLayout(central)

        status_box = QtWidgets.QGroupBox("Status")
        status_layout = QtWidgets.QVBoxLayout(status_box)
        self._telescope_status = QtWidgets.QLabel("Telescope: Not connected")
        self._camera_status = QtWidgets.QLabel("Camera: Not connected")
        self._calibration_status = QtWidgets.QLabel("Finder Calibration: Not calibrated")
        self._telescope_status.setWordWrap(True)
        self._camera_status.setWordWrap(True)
        self._calibration_status.setWordWrap(True)
        status_layout.addWidget(self._telescope_status)
        status_layout.addWidget(self._camera_status)
        status_layout.addWidget(self._calibration_status)

        left_panel = QtWidgets.QWidget()
        left_layout = QtWidgets.QVBoxLayout(left_panel)
        self._align_telescope_btn = QtWidgets.QPushButton(f"Align {MAIN_SCOPE_NAME}")
        self._align_telescope_btn.setMinimumHeight(140)
        self._align_telescope_btn.setStyleSheet(
            "QPushButton {"
            "background-color: #2d8f3f; color: white; font-size: 22px; font-weight: 600;"
            "border-radius: 10px; padding: 16px;"
            "}"
            "QPushButton:disabled { background-color: #5f7f66; }"
        )
        self._align_telescope_btn.clicked.connect(self._align_telescope)

        self._calibrate_finder_btn = QtWidgets.QPushButton("Calibrate Finder")
        self._calibrate_finder_btn.setMinimumHeight(52)
        self._calibrate_finder_btn.clicked.connect(self._open_alignment_wizard)

        left_layout.addWidget(self._align_telescope_btn)
        left_layout.addWidget(self._calibrate_finder_btn)
        left_layout.addStretch(1)

        image_panel = QtWidgets.QGroupBox("Latest Finder Image")
        image_layout = QtWidgets.QVBoxLayout(image_panel)
        self._image_view = _ZoomableImageView()
        self._image_view.setMinimumSize(640, 480)
        self._image_view.set_placeholder("No image")
        self._image_stats_label = QtWidgets.QLabel("Min: - | Mean: - | Max: -")
        self._image_stats_label.setAlignment(QtCore.Qt.AlignmentFlag.AlignLeft)
        self._image_stats_label.setWordWrap(False)
        self._image_stats_label.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding, QtWidgets.QSizePolicy.Policy.Fixed)
        self._image_stats_label.setMinimumHeight(22)
        self._image_stats_label.setMaximumHeight(22)
        stretch_row = QtWidgets.QHBoxLayout()
        self._stretch_label = QtWidgets.QLabel(f"Stretch: {self._settings.image_stretch_level}")
        self._stretch_label.setMinimumWidth(92)
        self._stretch_slider = QtWidgets.QSlider(QtCore.Qt.Orientation.Horizontal)
        self._stretch_slider.setRange(0, 100)
        self._stretch_slider.setSingleStep(1)
        self._stretch_slider.setPageStep(5)
        self._stretch_slider.setValue(self._settings.image_stretch_level)
        self._stretch_slider.setToolTip("Adjust display stretch for the latest finder image.")
        self._stretch_slider.valueChanged.connect(self._on_stretch_changed)
        self._reset_view_btn = QtWidgets.QPushButton("Reset View")
        self._reset_view_btn.setToolTip("Reset zoom and pan to fit the image.")
        self._reset_view_btn.clicked.connect(self._image_view.reset_view)
        stretch_row.addWidget(self._stretch_label)
        stretch_row.addWidget(self._stretch_slider, 1)
        stretch_row.addWidget(self._reset_view_btn)
        image_layout.addWidget(self._image_view)
        image_layout.addWidget(self._image_stats_label)
        image_layout.addLayout(stretch_row)
        image_layout.setStretch(0, 1)
        image_layout.setStretch(1, 0)
        image_layout.setStretch(2, 0)

        body = QtWidgets.QHBoxLayout()
        body.addWidget(left_panel, 0)
        body.addWidget(image_panel, 1)

        bottom_row = QtWidgets.QHBoxLayout()
        self._scope_settings_btn = QtWidgets.QPushButton("Telescope Settings")
        self._camera_settings_btn = QtWidgets.QPushButton("Camera Settings")
        self._app_settings_btn = QtWidgets.QPushButton("App Settings")
        self._test_solve_btn = QtWidgets.QPushButton("Test Plate Solve")
        self._exit_btn = QtWidgets.QPushButton("Exit App")
        self._scope_settings_btn.clicked.connect(self._open_telescope_settings)
        self._camera_settings_btn.clicked.connect(self._open_camera_settings)
        self._app_settings_btn.clicked.connect(self._open_app_settings)
        self._test_solve_btn.clicked.connect(self._test_plate_solve)
        self._exit_btn.clicked.connect(self._exit_app)

        bottom_row.addWidget(self._scope_settings_btn)
        bottom_row.addWidget(self._camera_settings_btn)
        bottom_row.addWidget(self._app_settings_btn)
        bottom_row.addWidget(self._test_solve_btn)
        bottom_row.addStretch(1)
        bottom_row.addWidget(self._exit_btn)

        root.addWidget(status_box)
        root.addLayout(body)
        root.addLayout(bottom_row)

        self.setCentralWidget(central)

    def _load_settings(self) -> PersistentSettings:
        defaults = PersistentSettings(telescope_history=self._default_telescope_history())
        try:
            if not self._settings_path.exists():
                return defaults
            payload = json.loads(self._settings_path.read_text(encoding="utf-8"))
            loaded = PersistentSettings.from_dict(payload)
            history = self._default_telescope_history()
            for item in loaded.telescope_history or []:
                if item not in history:
                    history.append(item)
            loaded.telescope_history = history
            return loaded
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to load settings file %s: %s", self._settings_path, exc)
            return defaults

    def _save_settings(self) -> None:
        self._settings_path.parent.mkdir(parents=True, exist_ok=True)
        self._settings_path.write_text(json.dumps(self._settings.to_dict(), indent=2), encoding="utf-8")

    def _default_telescope_history(self) -> list[str]:
        history: list[str] = []
        for _name, host, port, device in KNOWN_ALPACA_TELESCOPES:
            key = f"{host}:{port}:{device}"
            if key not in history:
                history.append(key)
        return history

    def _apply_initial_window_size(self) -> None:
        width = max(800, int(self._settings.app_window_width))
        height = max(600, int(self._settings.app_window_height))

        screen = QtGui.QGuiApplication.primaryScreen()
        if screen is not None:
            available = screen.availableGeometry()
            width = min(width, max(800, available.width()))
            height = min(height, max(600, available.height()))

        self._settings.app_window_width = width
        self._settings.app_window_height = height
        self.resize(width, height)

    def _short_error(self, message: str | None, max_len: int = 140) -> str:
        if not message:
            return ""
        compact = " ".join(str(message).split())
        if len(compact) <= max_len:
            return compact
        return f"{compact[: max_len - 3]}..."

    def _apply_retry_interval(self) -> None:
        interval_ms = int(max(5, self._settings.reconnect_interval_s) * 1000)
        self._telescope_retry_timer.setInterval(interval_ms)
        self._camera_retry_timer.setInterval(interval_ms)

        if self._settings.telescope_selected is not None:
            self._telescope_retry_timer.start()
        else:
            self._telescope_retry_timer.stop()

        if self._settings.camera_selected != "none":
            self._camera_retry_timer.start()
        else:
            self._camera_retry_timer.stop()

    def _resolve_sample_image_path(self, configured_path: str) -> Path:
        candidate = Path(configured_path)
        if candidate.is_absolute():
            return candidate
        return Path(__file__).resolve().parent.parent / candidate

    def _load_sample_frame(self) -> Frame:
        if self._sample_image_path.suffix.lower() in {".fits", ".fit", ".fts"}:
            try:
                with fits.open(
                    str(self._sample_image_path),
                    ignore_missing_simple=True,
                    ignore_missing_end=True,
                    output_verify="ignore",
                    memmap=False,
                ) as hdul:
                    data = np.asarray(hdul[0].data)
                if data.ndim > 2:
                    data = np.asarray(data[0])
                if data.ndim != 2:
                    raise RuntimeError(f"FITS sample image must be 2D: {self._sample_image_path}")
                data = np.asarray(data)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Failed to parse FITS sample in app, solver will use source path directly: %s", exc)
                data = np.zeros((64, 64), dtype=np.uint16)
        else:
            image = QtGui.QImage(str(self._sample_image_path))
            if image.isNull():
                raise RuntimeError(f"Failed to load sample image: {self._sample_image_path}")
            gray = image.convertToFormat(QtGui.QImage.Format.Format_Grayscale8)
            width = gray.width()
            height = gray.height()
            buf = gray.bits().tobytes()
            data = np.frombuffer(buf, dtype=np.uint8).reshape((height, width)).astype(np.uint16)

        return Frame(data=data, captured_at_utc=now_utc_iso(), source_path=str(self._sample_image_path), true_coords=None)

    def _parse_endpoint(self, endpoint: str | None) -> tuple[str, int, int] | None:
        if endpoint is None:
            return None
        parts = endpoint.split(":")
        if len(parts) < 2:
            return None
        host = parts[0].strip() or DEFAULT_ALPACA_HOST
        try:
            port = int(parts[1])
            device = int(parts[2]) if len(parts) > 2 else DEFAULT_ALPACA_DEVICE_NUMBER
        except ValueError:
            return None
        return (host, port, device)

    def _selected_telescope_name(self) -> str:
        selected = self._settings.telescope_selected
        if selected is None:
            return "No Telescope"
        return selected

    def _disconnect_telescope(self) -> None:
        self._telescope = None

    def _disconnect_camera(self) -> None:
        if self._camera is not None and hasattr(self._camera, "close"):
            try:
                self._camera.close()  # type: ignore[attr-defined]
            except Exception as exc:  # noqa: BLE001
                logger.warning("Failed to close camera backend: %s", exc)
        self._camera = None

    def _attempt_telescope_connect(self) -> None:
        endpoint = self._parse_endpoint(self._settings.telescope_selected)
        if endpoint is None:
            self._disconnect_telescope()
            self._telescope_last_error = None
            return

        if self._telescope is not None:
            try:
                if self._telescope.is_connected():
                    return
            except Exception:
                self._telescope = None

        if self._telescope_connect_in_progress:
            return

        host, port, device = endpoint
        endpoint_text = f"{host}:{port}:{device}"

        self._telescope_connect_in_progress = True
        self._telescope_connect_thread = QtCore.QThread(self)
        self._telescope_connect_worker = _TelescopeConnectWorker(
            endpoint_text=endpoint_text,
            host=host,
            port=port,
            device=device,
            epoch=self._epoch,
            timeout_s=TIMEOUTS.telescope_connect_s,
        )
        self._telescope_connect_worker.moveToThread(self._telescope_connect_thread)
        self._telescope_connect_thread.started.connect(self._telescope_connect_worker.run)
        self._telescope_connect_worker.connected.connect(self._on_telescope_connected)
        self._telescope_connect_worker.failed.connect(self._on_telescope_connect_failed)
        self._telescope_connect_worker.finished.connect(self._on_telescope_connect_finished)
        self._telescope_connect_worker.finished.connect(self._telescope_connect_thread.quit)
        self._telescope_connect_worker.finished.connect(self._telescope_connect_worker.deleteLater)
        self._telescope_connect_thread.finished.connect(self._telescope_connect_thread.deleteLater)
        self._telescope_connect_thread.start()

    @QtCore.Slot(object, str)
    def _on_telescope_connected(self, scope: object, endpoint_text: str) -> None:
        if self._settings.telescope_selected != endpoint_text:
            return

        self._telescope = scope  # type: ignore[assignment]
        self._telescope_last_error = None
        logger.info("Connected telescope %s", endpoint_text)
        self._refresh_status_lines()

    @QtCore.Slot(str, str)
    def _on_telescope_connect_failed(self, error_text: str, endpoint_text: str) -> None:
        if self._settings.telescope_selected != endpoint_text:
            return

        self._telescope_last_error = error_text
        self._telescope = None
        logger.warning("Telescope connect failed (%s): %s", endpoint_text, error_text)
        self._refresh_status_lines()

    @QtCore.Slot()
    def _on_telescope_connect_finished(self) -> None:
        self._telescope_connect_in_progress = False
        self._telescope_connect_worker = None
        self._telescope_connect_thread = None

    def _mount_provider(self) -> Coordinates:
        if self._telescope is not None:
            try:
                return self._telescope.get_coordinates(timeout_s=1.0)
            except Exception:
                pass
        return Coordinates(ra_deg=0.0, dec_deg=0.0, epoch=self._epoch)

    def _astap_hint_coordinates(self) -> Coordinates | None:
        if self._telescope is None:
            return None
        try:
            if not self._telescope.is_connected():
                return None
            return self._telescope.get_coordinates(timeout_s=1.0)
        except Exception:  # noqa: BLE001
            # If mount cannot report coordinates (e.g., not roughly aligned), use blind solve.
            return None

    def _apply_camera_settings_to_backend(self) -> None:
        if self._camera is None:
            return
        self._camera.set_exposure_ms(self._settings.camera_exposure_ms)
        self._camera.set_gain(self._settings.camera_gain)
        if isinstance(self._camera, ZwoCameraClient):
            self._camera.set_binning(self._settings.camera_binning)
            try:
                selected = CameraDataType(self._settings.camera_data_type)
            except ValueError:
                selected = CameraDataType.RAW16
            if selected == CameraDataType.RGB24:
                # RGB24 is intentionally disabled in this app's single-plane image pipeline.
                selected = CameraDataType.RAW16
                self._settings.camera_data_type = CameraDataType.RAW16.value
            self._camera.set_data_type(selected)

    def _attempt_camera_connect(self) -> None:
        selected = self._settings.camera_selected
        if selected == "none":
            self._disconnect_camera()
            self._camera_last_error = None
            return

        if self._camera is not None:
            try:
                if self._camera.is_connected():
                    return
            except Exception:
                self._disconnect_camera()

        try:
            if selected == "zwo":
                self._camera = ZwoCameraClient(settings=ZwoCameraSettings(camera_index=self._settings.zwo_camera_index))
            elif selected == "simulator":
                self._camera = SimulatedCameraClient(mount_provider=self._mount_provider, epoch=self._epoch)
            else:
                self._camera = None
                return

            self._apply_camera_settings_to_backend()
        except Exception as exc:  # noqa: BLE001
            self._camera_last_error = str(exc)
            self._disconnect_camera()
            logger.warning("Camera connect failed (%s): %s", selected, exc)
            return

        self._camera_last_error = None
        logger.info("Connected camera backend %s", selected)

    def _open_telescope_settings(self) -> None:
        dialog = TelescopeSettingsDialog(
            history=list(self._settings.telescope_history or []),
            selected=self._settings.telescope_selected,
            parent=self,
        )
        if dialog.exec() != QtWidgets.QDialog.DialogCode.Accepted:
            return

        self._settings.telescope_selected = dialog.selected_endpoint
        self._settings.telescope_history = dialog.history
        self._save_settings()

        if self._settings.telescope_selected is None:
            self._disconnect_telescope()
            self._telescope_retry_timer.stop()
        else:
            self._attempt_telescope_connect()
            self._telescope_retry_timer.start()
        self._refresh_status_lines()

    def _open_camera_settings(self) -> None:
        dialog = CameraSettingsDialog(self._settings, self)
        if dialog.exec() != QtWidgets.QDialog.DialogCode.Accepted:
            return

        dialog.apply_to(self._settings)
        self._save_settings()

        self._disconnect_camera()
        if self._settings.camera_selected == "none":
            self._camera_retry_timer.stop()
        else:
            self._attempt_camera_connect()
            self._camera_retry_timer.start()

        if self._settings.camera_looping:
            self._live_timer.start()
        else:
            self._live_timer.stop()

        self._refresh_status_lines()

    def _open_app_settings(self) -> None:
        dialog = AppSettingsDialog(self._settings, self)
        if dialog.exec() != QtWidgets.QDialog.DialogCode.Accepted:
            return

        dialog.apply_to(self._settings)
        self._apply_logging_level(self._settings.logging_level)
        self._epoch = self._settings.app_epoch
        self._solver = AstapPlateSolver(
            astap_executable=self._settings.astap_executable,
            downsample_factor=self._settings.astap_downsize_factor,
            approximate_coords_provider=self._astap_hint_coordinates,
        )
        self._apply_retry_interval()
        self._save_settings()
        self._refresh_status_lines()

    def _apply_logging_level(self, level_name: str) -> None:
        level_value = getattr(logging, level_name.upper(), logging.INFO)
        logging.getLogger().setLevel(level_value)
        logger.info("Logging level set to %s", logging.getLevelName(level_value))

    def _toggle_live_loop(self, enabled: bool) -> None:
        self._settings.camera_looping = enabled
        self._save_settings()
        if enabled:
            self._live_timer.start()
            self._capture_latest_frame()
        else:
            self._live_timer.stop()

    def _capture_latest_frame(self) -> None:
        if self._capture_in_progress:
            return
        if self._camera is None:
            return

        self._capture_in_progress = True

        self._capture_thread = QtCore.QThread(self)
        self._capture_worker = _CaptureWorker(self._capture_frame_sync)
        self._capture_worker.moveToThread(self._capture_thread)

        self._capture_thread.started.connect(self._capture_worker.run)
        self._capture_worker.frame_ready.connect(self._on_capture_worker_frame)
        self._capture_worker.error.connect(self._on_capture_worker_error)
        self._capture_worker.finished.connect(self._on_capture_worker_finished)
        self._capture_worker.finished.connect(self._capture_thread.quit)
        self._capture_worker.finished.connect(self._capture_worker.deleteLater)
        self._capture_thread.finished.connect(self._capture_thread.deleteLater)

        self._capture_thread.start()

    def _capture_frame_sync(self) -> Frame:
        if self._camera is None:
            raise RuntimeError("Camera is not configured")

        if self._settings.camera_selected == "simulator" and self._sample_image_path.exists():
            return self._load_sample_frame()

        return self._camera.capture_frame(timeout_s=TIMEOUTS.camera_capture_s)

    @QtCore.Slot(object)
    def _on_capture_worker_frame(self, frame: object) -> None:
        if not isinstance(frame, Frame):
            return

        self._latest_frame = frame
        self._render_frame(frame)
        self._refresh_status_lines()

    @QtCore.Slot(str)
    def _on_capture_worker_error(self, message: str) -> None:
        logger.error("Capture failed: %s", message)

    @QtCore.Slot()
    def _on_capture_worker_finished(self) -> None:
        self._capture_in_progress = False
        self._capture_worker = None
        self._capture_thread = None

    def _wait_for_capture_finish(self) -> bool:
        if not self._capture_in_progress:
            return True

        deadline = time.monotonic() + TIMEOUTS.camera_capture_s + 2.0
        while self._capture_in_progress and time.monotonic() < deadline:
            QtWidgets.QApplication.processEvents(QtCore.QEventLoop.ProcessEventsFlag.AllEvents, 50)
            time.sleep(0.01)
        return not self._capture_in_progress

    def _render_frame(self, frame: Frame) -> None:
        data = frame.data
        if not isinstance(data, np.ndarray):
            self._image_view.set_placeholder("Unsupported image data")
            self._image_stats_label.setText("Min: - | Mean: - | Max: -")
            return

        stretched = self._stretch_image(data)
        stretched = self._apply_display_smoothing(stretched)
        h, w = stretched.shape
        qimg = QtGui.QImage(stretched.data, w, h, w, QtGui.QImage.Format.Format_Grayscale8).copy()
        pixmap = QtGui.QPixmap.fromImage(qimg)
        self._image_view.set_pixmap(pixmap)
        self._update_image_stats(data)

    def _apply_display_smoothing(self, stretched: np.ndarray) -> np.ndarray:
        # RAW Bayer-like frames can show checkerboard aliasing when displayed at reduced scale.
        # Apply a light 2x2 average for display only; solve/capture data is unchanged.
        if self._settings.camera_data_type != CameraDataType.RAW16.value:
            return stretched

        if stretched.ndim != 2 or stretched.shape[0] < 2 or stretched.shape[1] < 2:
            return stretched

        arr = stretched.astype(np.uint16, copy=False)
        smoothed = arr.copy()
        smoothed[1:, 1:] = (
            arr[1:, 1:] + arr[:-1, 1:] + arr[1:, :-1] + arr[:-1, :-1]
        ) // 4
        smoothed[0, :] = smoothed[1, :]
        smoothed[:, 0] = smoothed[:, 1]
        return smoothed.astype(np.uint8, copy=False)

    def _update_image_stats(self, image: np.ndarray) -> None:
        arr = np.asarray(image)
        if arr.size == 0:
            self._image_stats_label.setText("Min: - | Mean: - | Max: -")
            return

        min_val = float(np.min(arr))
        max_val = float(np.max(arr))
        mean_val = float(np.mean(arr))
        min_count = int(np.count_nonzero(arr == min_val))
        max_count = int(np.count_nonzero(arr == max_val))

        if np.issubdtype(arr.dtype, np.integer):
            min_text = f"{int(min_val)}"
            max_text = f"{int(max_val)}"
        else:
            min_text = f"{min_val:.3f}"
            max_text = f"{max_val:.3f}"

        self._image_stats_label.setText(
            f"Min: {min_text} ({min_count}) | Mean: {mean_val:.2f} | Max: {max_text} ({max_count})"
        )

    @QtCore.Slot(int)
    def _on_stretch_changed(self, value: int) -> None:
        level = min(100, max(0, int(value)))
        self._settings.image_stretch_level = level
        self._stretch_label.setText(f"Stretch: {level}")
        if self._latest_frame is not None:
            self._render_frame(self._latest_frame)

    def _stretch_percentiles(self) -> tuple[float, float]:
        level = float(min(100, max(0, self._settings.image_stretch_level)))
        if level <= 50.0:
            t = level / 50.0
            low_pct = 0.0 + (5.0 * t)
            high_pct = 100.0 - (0.5 * t)
        else:
            t = (level - 50.0) / 50.0
            low_pct = 5.0 + (15.0 * t)
            high_pct = 99.5 - (1.5 * t)
        return low_pct, high_pct

    def resizeEvent(self, event: QtGui.QResizeEvent) -> None:
        super().resizeEvent(event)
        if not self.isMaximized() and not self.isFullScreen():
            width = int(self.width())
            height = int(self.height())
            screen = self.screen() or QtGui.QGuiApplication.primaryScreen()
            if screen is not None:
                available = screen.availableGeometry()
                width = min(width, max(800, available.width()))
                height = min(height, max(600, available.height()))
            self._settings.app_window_width = max(800, width)
            self._settings.app_window_height = max(600, height)
        if self._latest_frame is not None:
            self._render_frame(self._latest_frame)

    def _stretch_image(self, image: np.ndarray) -> np.ndarray:
        arr = image.astype(np.float32)
        lo_pct, hi_pct = self._stretch_percentiles()
        lo = np.percentile(arr, lo_pct)
        hi = np.percentile(arr, hi_pct)
        if hi <= lo:
            # Uniform image: render bright if data is non-zero (saturated edge case), otherwise black.
            fill_value = 255 if float(np.max(arr)) > 0.0 else 0
            return np.full(arr.shape, fill_value, dtype=np.uint8)
        scaled = np.clip((arr - lo) / (hi - lo), 0, 1) * 255.0
        return scaled.astype(np.uint8)

    def _open_alignment_wizard(self) -> None:
        if self._telescope is None or self._solver is None:
            QtWidgets.QMessageBox.warning(self, "Unavailable", "Telescope and plate solver must be connected first.")
            return

        dialog = AlignmentWizardDialog(
            telescope=self._telescope,
            solver=self._solver,
            frame_provider=self._capture_fresh_alignment_frame,
            epoch=self._epoch,
            parent=self,
        )
        if dialog.exec() != QtWidgets.QDialog.DialogCode.Accepted:
            return

        record = dialog.calibration_record
        if record is None:
            return

        self._store.save_new(record)
        self._store.set_manual_invalidated(False)
        self._latest_calibration = record
        logger.info(
            "Finder calibration completed star=%s mount_ra=%.6f mount_dec=%.6f finder_ra=%.6f finder_dec=%.6f offset_ra=%.6f offset_dec=%.6f",
            record.star_name,
            record.mount_ra_deg,
            record.mount_dec_deg,
            record.finder_ra_deg,
            record.finder_dec_deg,
            record.offset_ra_deg,
            record.offset_dec_deg,
        )
        self._refresh_status_lines()

    def _capture_fresh_alignment_frame(self) -> Frame:
        if self._camera is None:
            raise RuntimeError("Camera is not configured")

        if not self._wait_for_capture_finish():
            raise TimeoutError("Timed out waiting for ongoing capture to finish")

        if self._telescope is not None and self._telescope.is_slewing():
            raise RuntimeError("Telescope is still moving")

        settle_deadline = time.monotonic() + TIMEOUTS.alignment_settle_s
        while time.monotonic() < settle_deadline:
            if self._telescope is not None and self._telescope.is_slewing():
                raise RuntimeError("Telescope moved during settle time")
            QtWidgets.QApplication.processEvents(QtCore.QEventLoop.ProcessEventsFlag.AllEvents, 50)
            time.sleep(0.01)

        frame = self._capture_frame_sync()
        self._latest_frame = frame
        self._render_frame(frame)
        self._refresh_status_lines()
        return frame

    def _align_telescope(self) -> None:
        if self._telescope is None or self._solver is None:
            QtWidgets.QMessageBox.warning(self, "Unavailable", "Telescope and plate solver must be connected first.")
            return
        if self._latest_calibration is None:
            QtWidgets.QMessageBox.warning(self, "No Finder Calibration", "Run Calibrate Finder first.")
            return
        if self._store.is_manual_invalidated():
            QtWidgets.QMessageBox.warning(self, "Calibration Invalid", "Finder calibration was manually invalidated.")
            return

        proceed = QtWidgets.QMessageBox.question(
            self,
            f"Align {MAIN_SCOPE_NAME}",
            "This process will:\n\n"
            "1. Capture image (wait for current capture to finish, or trigger a new capture)\n"
            "2. Plate solve the captured image\n"
            "3. Show solved coordinates and corrected sync coordinates before sending\n\n"
            "Continue?",
            QtWidgets.QMessageBox.StandardButton.Ok | QtWidgets.QMessageBox.StandardButton.Cancel,
            QtWidgets.QMessageBox.StandardButton.Ok,
        )
        if proceed != QtWidgets.QMessageBox.StandardButton.Ok:
            return

        if self._capture_in_progress:
            if not self._wait_for_capture_finish():
                QtWidgets.QMessageBox.warning(self, "Capture Busy", "Timed out waiting for current capture to finish.")
                return
            frame = self._latest_frame
            if frame is None:
                QtWidgets.QMessageBox.warning(self, "Capture Failed", "Capture finished but no frame is available.")
                return
        else:
            try:
                frame = self._capture_frame_sync()
            except Exception as exc:  # noqa: BLE001
                logger.exception("Capture failed during telescope alignment")
                QtWidgets.QMessageBox.warning(self, "Capture Failed", str(exc))
                return
            self._latest_frame = frame
            self._render_frame(frame)

        solve = self._solver.solve(frame, timeout_s=TIMEOUTS.plate_solve_s)
        self._latest_solve = solve

        if not solve.success or solve.coordinates is None:
            QtWidgets.QMessageBox.warning(self, "Plate Solve Failed", solve.message or "Unknown solver error")
            self._refresh_status_lines()
            return

        solved = solve.coordinates
        target_ra = wrap_ra_deg(solved.ra_deg + self._latest_calibration.offset_ra_deg)
        target_dec = clamp_dec_deg(solved.dec_deg + self._latest_calibration.offset_dec_deg)
        target = Coordinates(ra_deg=target_ra, dec_deg=target_dec, epoch=self._epoch)

        metrics_text = format_plate_solve_metrics(solve.metrics)
        review_text = (
            "Step 3: Review before sending alignment\n\n"
            "Plate solve solution:\n"
            f"RA {format_ra_deg_with_hms(solved.ra_deg, precision=6)}\n"
            f"Dec {format_dec_deg_with_dms(solved.dec_deg, precision=6)}\n\n"
            "Corrected sync to send:\n"
            f"RA {format_ra_deg_with_hms(target.ra_deg, precision=6)}\n"
            f"Dec {format_dec_deg_with_dms(target.dec_deg, precision=6)}"
        )
        if metrics_text:
            review_text = f"{review_text}\n\n{metrics_text}"

        review_box = QtWidgets.QMessageBox(self)
        review_box.setIcon(QtWidgets.QMessageBox.Icon.Information)
        review_box.setWindowTitle(f"Review {MAIN_SCOPE_NAME} Alignment")
        review_box.setText(review_text)
        send_btn = review_box.addButton(f"Send Alignment to {MAIN_SCOPE_NAME}", QtWidgets.QMessageBox.ButtonRole.AcceptRole)
        review_box.addButton(QtWidgets.QMessageBox.StandardButton.Cancel)
        review_box.setDefaultButton(QtWidgets.QMessageBox.StandardButton.Cancel)
        review_box.exec()
        if review_box.clickedButton() is not send_btn:
            return

        try:
            self._telescope.sync_to_coordinates(target, timeout_s=TIMEOUTS.telescope_command_s)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Telescope alignment sync failed")
            QtWidgets.QMessageBox.critical(self, "Sync Failed", str(exc))
            return

        logger.info(
            "Main scope alignment sent scope=%s solved_ra=%.6f solved_dec=%.6f sent_ra=%.6f sent_dec=%.6f cal_star=%s cal_offset_ra=%.6f cal_offset_dec=%.6f",
            MAIN_SCOPE_NAME,
            solved.ra_deg,
            solved.dec_deg,
            target.ra_deg,
            target.dec_deg,
            self._latest_calibration.star_name,
            self._latest_calibration.offset_ra_deg,
            self._latest_calibration.offset_dec_deg,
        )

        QtWidgets.QMessageBox.information(
            self,
            f"{MAIN_SCOPE_NAME} Aligned",
            f"Alignment sent to {MAIN_SCOPE_NAME}:\n"
            f"RA {format_ra_deg_with_hms(target.ra_deg, precision=5)}\n"
            f"Dec {format_dec_deg_with_dms(target.dec_deg, precision=5)}",
        )
        self._refresh_status_lines()

    def _update_action_buttons(self, telescope_connected: bool, camera_connected: bool) -> None:
        calibration_ready = self._latest_calibration is not None and self._store.is_manual_invalidated() is False

        align_reasons: list[str] = []
        if not telescope_connected:
            align_reasons.append("telescope is not connected")
        if not camera_connected:
            align_reasons.append("camera is not connected")
        if not calibration_ready:
            align_reasons.append("finder calibration is missing or invalid")

        if align_reasons:
            self._align_telescope_btn.setEnabled(False)
            self._align_telescope_btn.setToolTip(f"Align is unavailable: {', '.join(align_reasons)}.")
        else:
            self._align_telescope_btn.setEnabled(True)
            self._align_telescope_btn.setToolTip(f"Capture, solve, and align {MAIN_SCOPE_NAME}.")

        calibrate_reasons: list[str] = []
        if not telescope_connected:
            calibrate_reasons.append("telescope is not connected")
        if not camera_connected:
            calibrate_reasons.append("camera is not connected")

        if calibrate_reasons:
            self._calibrate_finder_btn.setEnabled(False)
            self._calibrate_finder_btn.setToolTip(f"Calibration is unavailable: {', '.join(calibrate_reasons)}.")
        else:
            self._calibrate_finder_btn.setEnabled(True)
            self._calibrate_finder_btn.setToolTip("Run the calibration wizard for finder-to-main offset.")

    def _test_plate_solve(self) -> None:
        if self._solver is None:
            QtWidgets.QMessageBox.warning(self, "Unavailable", "Plate solver is not configured.")
            return

        if not self._test_solve_btn.isEnabled():
            return

        self._test_solve_btn.setEnabled(False)
        QtWidgets.QApplication.processEvents(QtCore.QEventLoop.ProcessEventsFlag.AllEvents, 50)

        try:
            if self._capture_in_progress:
                if not self._wait_for_capture_finish():
                    QtWidgets.QMessageBox.warning(self, "Capture Busy", "Timed out waiting for current capture to finish.")
                    return
                frame = self._latest_frame
                if frame is None:
                    QtWidgets.QMessageBox.warning(self, "Capture Failed", "Capture finished but no frame is available.")
                    return
            else:
                try:
                    frame = self._capture_frame_sync()
                except Exception as exc:  # noqa: BLE001
                    logger.exception("Capture failed during plate solve test")
                    QtWidgets.QMessageBox.warning(self, "Capture Failed", str(exc))
                    return
                self._latest_frame = frame
                self._render_frame(frame)

            solve = self._solver.solve(frame, timeout_s=TIMEOUTS.plate_solve_s)
            self._latest_solve = solve
            self._refresh_status_lines()

            if not solve.success or solve.coordinates is None:
                QtWidgets.QMessageBox.warning(self, "Plate Solve Failed", solve.message or "Unknown solver error")
                return

            coords = solve.coordinates
            details = (
                f"RA {format_ra_deg_with_hms(coords.ra_deg, precision=6)}\n"
                f"Dec {format_dec_deg_with_dms(coords.dec_deg, precision=6)}"
            )
            metrics_text = format_plate_solve_metrics(solve.metrics)
            if metrics_text:
                details = f"{details}\n\n{metrics_text}"

            QtWidgets.QMessageBox.information(
                self,
                "Plate Solve Success",
                details,
            )
        finally:
            self._test_solve_btn.setEnabled(True)
            QtWidgets.QApplication.processEvents(QtCore.QEventLoop.ProcessEventsFlag.AllEvents, 50)

    def _refresh_status_lines(self) -> None:
        telescope_connected = False
        if self._settings.telescope_selected is None:
            self._telescope_status.setText("Telescope: No telescope selected")
        elif self._telescope is None:
            suffix = f" | retrying in {self._settings.reconnect_interval_s}s"
            if self._telescope_last_error:
                self._telescope_status.setText(
                    f"Telescope: {self._selected_telescope_name()} disconnected ({self._short_error(self._telescope_last_error)}){suffix}"
                )
            else:
                self._telescope_status.setText(f"Telescope: {self._selected_telescope_name()} disconnected{suffix}")
        else:
            try:
                connected = self._telescope.is_connected()
                if not connected:
                    # Drop stale object so only timer-driven reconnect is attempted.
                    self._telescope = None
                    self._telescope_status.setText(
                        f"Telescope: {self._selected_telescope_name()} disconnected | retrying in {self._settings.reconnect_interval_s}s"
                    )
                else:
                    telescope_connected = True
                    coords = self._telescope.get_coordinates(timeout_s=1.0)
                    slewing = self._telescope.is_slewing()
                    slew_text = " | slewing" if slewing else ""
                    self._telescope_status.setText(
                        f"Telescope: {self._selected_telescope_name()} | "
                        f"RA {format_ra_deg_with_hms(coords.ra_deg, precision=5)} | "
                        f"Dec {format_dec_deg_with_dms(coords.dec_deg, precision=5)}"
                        f"{slew_text}"
                    )
            except Exception as exc:  # noqa: BLE001
                # Avoid repeated blocking reads on a dead endpoint; retry via reconnect timer.
                self._telescope = None
                self._telescope_status.setText(
                    "Telescope: "
                    f"{self._selected_telescope_name()} disconnected ({self._short_error(str(exc))}) | "
                    f"retrying in {self._settings.reconnect_interval_s}s"
                )

        loop_state = "looping" if self._live_timer.isActive() else "stopped"
        camera_connected = False
        camera_name = {
            "none": "No camera selected",
            "zwo": "ZWO camera",
            "simulator": "Simulator camera",
        }.get(self._settings.camera_selected, self._settings.camera_selected)

        if self._settings.camera_selected == "none":
            self._camera_status.setText("Camera: No camera selected")
        elif self._camera is None:
            if self._camera_last_error:
                self._camera_status.setText(
                    f"Camera: {camera_name} disconnected ({self._short_error(self._camera_last_error)}) | "
                    f"exp {self._settings.camera_exposure_ms} ms | gain {self._settings.camera_gain} | {loop_state}"
                )
            else:
                self._camera_status.setText(
                    f"Camera: {camera_name} disconnected | exp {self._settings.camera_exposure_ms} ms | "
                    f"gain {self._settings.camera_gain} | {loop_state}"
                )
        else:
            try:
                camera_connected = bool(self._camera.is_connected())
            except Exception as exc:  # noqa: BLE001
                camera_connected = False
                self._camera_last_error = str(exc)
                self._disconnect_camera()

            if camera_connected:
                self._camera_status.setText(
                    f"Camera: {camera_name} connected | exp {self._settings.camera_exposure_ms} ms | "
                    f"gain {self._settings.camera_gain} | {loop_state}"
                )
            else:
                self._camera_status.setText(
                    f"Camera: {camera_name} disconnected | exp {self._settings.camera_exposure_ms} ms | "
                    f"gain {self._settings.camera_gain} | {loop_state}"
                )

        self._latest_calibration = self._store.load_latest()
        if self._latest_calibration is None:
            self._calibration_status.setText("Finder Calibration: Not calibrated")
        else:
            local_ts = self._format_local_calibration_time(self._latest_calibration.timestamp_utc)
            self._calibration_status.setText(
                "Finder Calibration: "
                f"{local_ts} | "
                f"offset RA {self._latest_calibration.offset_ra_deg:.5f}°, "
                f"Dec {self._latest_calibration.offset_dec_deg:.5f}°"
            )

        self._update_action_buttons(telescope_connected=telescope_connected, camera_connected=camera_connected)

    def _discover_telescopes(self, show_dialog: bool = True) -> list[DiscoveredTelescope]:
        # Discovery retained for future UI use; intentionally not exposed in the main UX.
        try:
            found = discover_alpaca_telescopes(
                numquery=ALPACA_DISCOVERY_NUMQUERY,
                timeout_s=ALPACA_DISCOVERY_TIMEOUT_S,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("Alpaca discovery failed")
            if show_dialog:
                QtWidgets.QMessageBox.warning(self, "Discovery Failed", str(exc))
            return []

        if show_dialog:
            QtWidgets.QMessageBox.information(self, "Discovery Complete", f"Found {len(found)} Alpaca telescope device(s).")
        return found

    def _format_local_calibration_time(self, timestamp_utc: str) -> str:
        try:
            parsed = datetime.fromisoformat(timestamp_utc.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            local = parsed.astimezone(USER_TIMEZONE)
            return local.strftime("%Y-%m-%d %H:%M:%S %Z")
        except Exception:  # noqa: BLE001
            return timestamp_utc

    def _exit_app(self) -> None:
        self.close()

    def closeEvent(self, event: QtGui.QCloseEvent) -> None:
        self._live_timer.stop()
        self._status_timer.stop()
        self._telescope_retry_timer.stop()
        self._camera_retry_timer.stop()

        if self._telescope_connect_thread is not None:
            self._telescope_connect_thread.quit()
            self._telescope_connect_thread.wait(1000)

        if self._capture_thread is not None:
            self._capture_thread.quit()
            self._capture_thread.wait(1000)

        self._disconnect_camera()
        self._disconnect_telescope()
        self._save_settings()
        super().closeEvent(event)


def run() -> int:
    log_path = configure_logging()
    logger.info("Starting application")

    app = QtWidgets.QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setOrganizationName("Powell Observatory ASKC")

    window = MainWindow()
    window.show()

    logger.info("Application started log_file=%s", log_path)
    return app.exec()
