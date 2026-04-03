from __future__ import annotations

import logging
import sys
from datetime import datetime, timezone

import numpy as np
from PySide6 import QtCore, QtGui, QtWidgets

from digital_finder.config import APP_NAME, DEFAULT_EPOCH, TIMEOUTS
from digital_finder.logging_setup import configure_logging
from digital_finder.models import CalibrationRecord, Coordinates, Frame, SolveResult, clamp_dec_deg, wrap_ra_deg
from digital_finder.services.alpaca_telescope import AlpacaTelescopeClient
from digital_finder.services.astap_solver import AstapPlateSolver
from digital_finder.services.interfaces import CameraClient, PlateSolver, TelescopeClient
from digital_finder.services.simulated import SimulatedCameraClient, SimulatedPlateSolver, SimulatedTelescopeClient
from digital_finder.storage import CalibrationStore
from digital_finder.ui.alignment_wizard import AlignmentWizardDialog

logger = logging.getLogger(__name__)


class MainWindow(QtWidgets.QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.resize(1100, 720)

        self._epoch = DEFAULT_EPOCH
        self._store = CalibrationStore()
        self._latest_calibration = self._store.load_latest()

        self._telescope: TelescopeClient | None = None
        self._camera: CameraClient | None = None
        self._solver: PlateSolver | None = None
        self._latest_frame: Frame | None = None
        self._latest_solve: SolveResult | None = None

        self._live_timer = QtCore.QTimer(self)
        self._live_timer.setInterval(2000)
        self._live_timer.timeout.connect(self._capture_latest_frame)

        self._status_timer = QtCore.QTimer(self)
        self._status_timer.setInterval(1000)
        self._status_timer.timeout.connect(self._refresh_dashboard)
        self._status_timer.start()

        self._build_ui()
        self._set_backend_mode("Simulator")
        self._refresh_dashboard()

    def _build_ui(self) -> None:
        central = QtWidgets.QWidget()
        root = QtWidgets.QVBoxLayout(central)

        dashboard = QtWidgets.QGroupBox("Dashboard")
        dash_layout = QtWidgets.QGridLayout(dashboard)

        self._mode_label = QtWidgets.QLabel("Mode: -")
        self._scope_label = QtWidgets.QLabel("Scope: -")
        self._coords_label = QtWidgets.QLabel("Scope RA/Dec: -")
        self._camera_label = QtWidgets.QLabel("Camera: -")
        self._frame_label = QtWidgets.QLabel("Latest frame: -")
        self._cal_label = QtWidgets.QLabel("Calibration: -")
        self._solve_label = QtWidgets.QLabel("Last solve: -")

        dash_layout.addWidget(self._mode_label, 0, 0)
        dash_layout.addWidget(self._scope_label, 0, 1)
        dash_layout.addWidget(self._camera_label, 0, 2)
        dash_layout.addWidget(self._coords_label, 1, 0)
        dash_layout.addWidget(self._frame_label, 1, 1)
        dash_layout.addWidget(self._solve_label, 1, 2)
        dash_layout.addWidget(self._cal_label, 2, 0, 1, 3)

        controls = QtWidgets.QGroupBox("Controls")
        ctrl_layout = QtWidgets.QGridLayout(controls)

        self._mode_combo = QtWidgets.QComboBox()
        self._mode_combo.addItems(["Simulator", "Alpaca+ASTAP (stub)"])
        self._mode_combo.currentTextChanged.connect(self._set_backend_mode)

        self._exp_spin = QtWidgets.QSpinBox()
        self._exp_spin.setRange(50, 30_000)
        self._exp_spin.setValue(1500)
        self._exp_spin.setSuffix(" ms")

        self._gain_spin = QtWidgets.QSpinBox()
        self._gain_spin.setRange(0, 600)
        self._gain_spin.setValue(120)

        self._apply_cam_btn = QtWidgets.QPushButton("Apply Camera Settings")
        self._apply_cam_btn.clicked.connect(self._apply_camera_settings)

        self._live_toggle_btn = QtWidgets.QPushButton("Start Live Loop")
        self._live_toggle_btn.setCheckable(True)
        self._live_toggle_btn.toggled.connect(self._toggle_live_loop)

        self._capture_btn = QtWidgets.QPushButton("Capture Now")
        self._capture_btn.clicked.connect(self._capture_latest_frame)

        self._align_btn = QtWidgets.QPushButton("Finder Alignment")
        self._align_btn.clicked.connect(self._open_alignment_wizard)

        self._cal_main_btn = QtWidgets.QPushButton("Calibrate Main Telescope")
        self._cal_main_btn.clicked.connect(self._calibrate_main_telescope)

        self._invalidate_btn = QtWidgets.QPushButton("Invalidate Calibration")
        self._invalidate_btn.clicked.connect(self._invalidate_calibration)

        ctrl_layout.addWidget(QtWidgets.QLabel("Backend mode"), 0, 0)
        ctrl_layout.addWidget(self._mode_combo, 0, 1)
        ctrl_layout.addWidget(QtWidgets.QLabel("Exposure"), 1, 0)
        ctrl_layout.addWidget(self._exp_spin, 1, 1)
        ctrl_layout.addWidget(QtWidgets.QLabel("Gain"), 2, 0)
        ctrl_layout.addWidget(self._gain_spin, 2, 1)
        ctrl_layout.addWidget(self._apply_cam_btn, 3, 0, 1, 2)
        ctrl_layout.addWidget(self._live_toggle_btn, 4, 0, 1, 2)
        ctrl_layout.addWidget(self._capture_btn, 5, 0, 1, 2)
        ctrl_layout.addWidget(self._align_btn, 6, 0, 1, 2)
        ctrl_layout.addWidget(self._cal_main_btn, 7, 0, 1, 2)
        ctrl_layout.addWidget(self._invalidate_btn, 8, 0, 1, 2)

        image_panel = QtWidgets.QGroupBox("Latest Finder Image")
        image_layout = QtWidgets.QVBoxLayout(image_panel)
        self._image_label = QtWidgets.QLabel("No image")
        self._image_label.setMinimumSize(640, 480)
        self._image_label.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        self._image_label.setStyleSheet("QLabel { background-color: #111; color: #eee; }")
        image_layout.addWidget(self._image_label)

        body = QtWidgets.QHBoxLayout()
        body.addWidget(controls, 0)
        body.addWidget(image_panel, 1)

        root.addWidget(dashboard)
        root.addLayout(body)

        self.setCentralWidget(central)

    def _set_backend_mode(self, mode: str) -> None:
        logger.info("Switching backend mode to %s", mode)
        if mode == "Simulator":
            scope = SimulatedTelescopeClient(epoch=self._epoch)
            camera = SimulatedCameraClient(
                mount_provider=lambda: scope.get_coordinates(timeout_s=TIMEOUTS.telescope_command_s),
                epoch=self._epoch,
            )
            solver = SimulatedPlateSolver()
        else:
            scope = AlpacaTelescopeClient(host="127.0.0.1", port=11111, device_number=0, epoch=self._epoch)
            camera = SimulatedCameraClient(
                mount_provider=lambda: Coordinates(ra_deg=0.0, dec_deg=0.0, epoch=self._epoch),
                epoch=self._epoch,
            )
            solver = AstapPlateSolver(astap_executable="astap.exe")
            QtWidgets.QMessageBox.information(
                self,
                "Backend Stub",
                "Alpaca+ASTAP backend is scaffolded but not fully wired yet. "
                "Simulator camera is active in this mode for UI testing.",
            )

        self._telescope = scope
        self._camera = camera
        self._solver = solver
        self._apply_camera_settings()
        self._refresh_dashboard()

    def _apply_camera_settings(self) -> None:
        if self._camera is None:
            return
        self._camera.set_exposure_ms(self._exp_spin.value())
        self._camera.set_gain(self._gain_spin.value())
        logger.info("Camera settings exposure_ms=%s gain=%s", self._exp_spin.value(), self._gain_spin.value())

    def _toggle_live_loop(self, enabled: bool) -> None:
        if enabled:
            self._live_timer.start()
            self._live_toggle_btn.setText("Stop Live Loop")
            self._capture_latest_frame()
        else:
            self._live_timer.stop()
            self._live_toggle_btn.setText("Start Live Loop")

    def _capture_latest_frame(self) -> None:
        if self._camera is None:
            return
        try:
            frame = self._camera.capture_frame(timeout_s=TIMEOUTS.camera_capture_s)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Capture failed")
            QtWidgets.QMessageBox.warning(self, "Capture Failed", str(exc))
            return

        self._latest_frame = frame
        self._render_frame(frame)
        self._refresh_dashboard()

    def _render_frame(self, frame: Frame) -> None:
        data = frame.data
        if not isinstance(data, np.ndarray):
            self._image_label.setText("Unsupported image data")
            return

        stretched = self._stretch_image(data)
        h, w = stretched.shape
        qimg = QtGui.QImage(stretched.data, w, h, w, QtGui.QImage.Format.Format_Grayscale8).copy()
        pixmap = QtGui.QPixmap.fromImage(qimg)
        self._image_label.setPixmap(pixmap.scaled(self._image_label.size(), QtCore.Qt.AspectRatioMode.KeepAspectRatio, QtCore.Qt.TransformationMode.SmoothTransformation))

    def resizeEvent(self, event: QtGui.QResizeEvent) -> None:
        super().resizeEvent(event)
        if self._latest_frame is not None:
            self._render_frame(self._latest_frame)

    def _stretch_image(self, image: np.ndarray) -> np.ndarray:
        arr = image.astype(np.float32)
        lo = np.percentile(arr, 5)
        hi = np.percentile(arr, 99.5)
        if hi <= lo:
            hi = lo + 1.0
        scaled = np.clip((arr - lo) / (hi - lo), 0, 1) * 255.0
        return scaled.astype(np.uint8)

    def _open_alignment_wizard(self) -> None:
        if self._telescope is None or self._solver is None:
            return
        dialog = AlignmentWizardDialog(
            telescope=self._telescope,
            solver=self._solver,
            frame_provider=self._get_or_capture_frame,
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
        self._refresh_dashboard()

    def _get_or_capture_frame(self) -> Frame:
        if self._latest_frame is None:
            self._capture_latest_frame()
        if self._latest_frame is None:
            raise RuntimeError("No frame available")
        return self._latest_frame

    def _calibrate_main_telescope(self) -> None:
        if self._telescope is None or self._solver is None:
            return
        if self._latest_calibration is None:
            QtWidgets.QMessageBox.warning(self, "No Calibration", "Run Finder Alignment first.")
            return
        if self._store.is_manual_invalidated():
            QtWidgets.QMessageBox.warning(self, "Calibration Invalid", "Calibration was manually invalidated.")
            return

        frame = self._get_or_capture_frame()
        solve = self._solver.solve(frame, timeout_s=TIMEOUTS.plate_solve_s)
        self._latest_solve = solve

        if not solve.success or solve.coordinates is None:
            QtWidgets.QMessageBox.warning(self, "Plate Solve Failed", solve.message or "Unknown solver error")
            self._refresh_dashboard()
            return

        solved = solve.coordinates
        target_ra = wrap_ra_deg(solved.ra_deg + self._latest_calibration.offset_ra_deg)
        target_dec = clamp_dec_deg(solved.dec_deg + self._latest_calibration.offset_dec_deg)
        target = Coordinates(ra_deg=target_ra, dec_deg=target_dec, epoch=self._epoch)

        try:
            self._telescope.sync_to_coordinates(target, timeout_s=TIMEOUTS.telescope_command_s)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Sync failed")
            QtWidgets.QMessageBox.critical(self, "Sync Failed", str(exc))
            return

        logger.info(
            "Main calibration sync sent ra=%.5f dec=%.5f from solve_ra=%.5f solve_dec=%.5f",
            target.ra_deg,
            target.dec_deg,
            solved.ra_deg,
            solved.dec_deg,
        )
        QtWidgets.QMessageBox.information(
            self,
            "Main Telescope Calibrated",
            f"Sent sync:\nRA {target.ra_deg:.5f} deg\nDec {target.dec_deg:.5f} deg",
        )
        self._refresh_dashboard()

    def _invalidate_calibration(self) -> None:
        self._store.set_manual_invalidated(True)
        self._refresh_dashboard()

    def _refresh_dashboard(self) -> None:
        mode = self._mode_combo.currentText()
        self._mode_label.setText(f"Mode: {mode} | Epoch: {self._epoch}")

        if self._telescope is not None:
            try:
                connected = self._telescope.is_connected()
                slewing = self._telescope.is_slewing()
                coords = self._telescope.get_coordinates(timeout_s=TIMEOUTS.telescope_command_s)
                self._scope_label.setText(f"Scope: {'Connected' if connected else 'Disconnected'} | Slewing: {slewing}")
                self._coords_label.setText(f"Scope RA/Dec: {coords.ra_deg:.5f} / {coords.dec_deg:.5f} deg")
            except Exception as exc:  # noqa: BLE001
                self._scope_label.setText(f"Scope: Error ({exc})")

        if self._camera is not None:
            self._camera_label.setText(f"Camera: {'Connected' if self._camera.is_connected() else 'Disconnected'}")

        if self._latest_frame is not None:
            self._frame_label.setText(f"Latest frame UTC: {self._latest_frame.captured_at_utc}")
        else:
            self._frame_label.setText("Latest frame: -")

        if self._latest_solve is not None:
            if self._latest_solve.success and self._latest_solve.coordinates is not None:
                c = self._latest_solve.coordinates
                self._solve_label.setText(f"Last solve: RA {c.ra_deg:.5f}, Dec {c.dec_deg:.5f}, conf={self._latest_solve.confidence}")
            else:
                self._solve_label.setText(f"Last solve failed: {self._latest_solve.message}")

        self._latest_calibration = self._store.load_latest()
        invalid = self._store.is_manual_invalidated()
        if self._latest_calibration is None:
            self._cal_label.setText("Calibration: none")
            self._cal_main_btn.setEnabled(False)
            return

        self._cal_label.setText(
            "Calibration: "
            f"{self._latest_calibration.timestamp_utc} | "
            f"Star {self._latest_calibration.star_name} | "
            f"Offset RA {self._latest_calibration.offset_ra_deg:.5f} deg, "
            f"Dec {self._latest_calibration.offset_dec_deg:.5f} deg | "
            f"confidence={self._latest_calibration.solve_confidence} | "
            f"invalidated={invalid}"
        )
        self._cal_main_btn.setEnabled(not invalid)


def run() -> int:
    log_path = configure_logging()
    logger.info("Starting application")

    app = QtWidgets.QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setOrganizationName("Observatory")

    window = MainWindow()
    window.show()

    logger.info("Application started log_file=%s", log_path)
    return app.exec()
