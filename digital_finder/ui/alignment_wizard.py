from __future__ import annotations

import logging

from PySide6 import QtCore, QtGui, QtWidgets

from digital_finder.config import TIMEOUTS
from digital_finder.models import (
    CalibrationRecord,
    Coordinates,
    Frame,
    SolveResult,
    format_horizontal_deg,
    format_plate_solve_metrics,
    format_dec_deg_with_dms,
    format_ra_deg_with_hms,
    now_utc_iso,
    radec_to_horizontal,
    wrap_ra_deg,
)
from digital_finder.services.interfaces import PlateSolver, TelescopeClient
from digital_finder.services.astap_solver import AstapPlateSolver
from digital_finder.stars import SAMPLE_CALIBRATION_STARS

logger = logging.getLogger(__name__)


class _SolveWorker(QtCore.QObject):
    solved = QtCore.Signal(object, object, object)
    failed = QtCore.Signal(str)
    finished = QtCore.Signal()
    progress = QtCore.Signal(str)

    def __init__(self, telescope: TelescopeClient, solver: PlateSolver, frame_provider, star) -> None:
        super().__init__()
        self._telescope = telescope
        self._solver = solver
        self._frame_provider = frame_provider
        self._star = star

    @QtCore.Slot()
    def run(self) -> None:
        try:
            self.progress.emit("Checking mount status...")
            if self._telescope.is_slewing():
                raise RuntimeError("Telescope is moving. Wait for motion to stop before solving.")

            self.progress.emit("Reading mount coordinates...")
            mount = self._telescope.get_coordinates(timeout_s=TIMEOUTS.telescope_command_s)
            self.progress.emit("Capturing alignment frame...")
            frame: Frame = self._frame_provider()
            if self._telescope.is_slewing():
                raise RuntimeError("Telescope moved during capture. Please try again.")

            self.progress.emit("Solving alignment image...")
            if isinstance(self._solver, AstapPlateSolver):
                solve = self._solver.solve_with_hint(frame, timeout_s=TIMEOUTS.plate_solve_s, hint=mount)
            else:
                solve = self._solver.solve(frame, timeout_s=TIMEOUTS.plate_solve_s)

            self.solved.emit(solve, mount, self._star)
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(str(exc))
        finally:
            self.finished.emit()


class AlignmentWizardDialog(QtWidgets.QDialog):
    def __init__(
        self,
        telescope: TelescopeClient,
        solver: PlateSolver,
        frame_provider,
        epoch: str,
        observatory_latitude_deg: float,
        observatory_longitude_deg: float,
        parent: QtWidgets.QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Finder Alignment")
        self.setModal(True)
        self.resize(520, 280)

        self._telescope = telescope
        self._solver = solver
        self._frame_provider = frame_provider
        self._epoch = epoch
        self._observatory_latitude_deg = observatory_latitude_deg
        self._observatory_longitude_deg = observatory_longitude_deg
        self._calibration_record: CalibrationRecord | None = None
        self._solve_thread: QtCore.QThread | None = None
        self._solve_worker: _SolveWorker | None = None
        self._solve_in_progress = False
        self._solve_cancelled = False
        self._cancel_requested = False
        self._pending_close = False
        self._pending_retry = False
        self._solve_timeout_timer = QtCore.QTimer(self)
        self._solve_timeout_timer.setSingleShot(True)
        self._solve_attempts = 0
        self._max_solve_attempts = 3

        self._status = QtWidgets.QLabel("Step 1: Select an alignment star.")
        self._status.setWordWrap(True)

        self._star_combo = QtWidgets.QComboBox()
        self._star_combo.addItem("Select star...")
        for star in SAMPLE_CALIBRATION_STARS:
            self._star_combo.addItem(
                f"{star.name} ("
                f"RA {format_ra_deg_with_hms(star.ra_deg, precision=3)}, "
                f"Dec {format_dec_deg_with_dms(star.dec_deg, precision=3)}, "
                f"{self._format_altaz_text(star.ra_deg, star.dec_deg, precision=2)}"
                f")",
                star,
            )

        self._slew_btn = QtWidgets.QPushButton("Slew Scope to Star")
        self._slew_btn.setEnabled(False)
        self._aligned_btn = QtWidgets.QPushButton("Star Is Aligned")
        self._aligned_btn.setEnabled(False)

        self._close_btn = QtWidgets.QPushButton("Cancel")

        button_row = QtWidgets.QHBoxLayout()
        button_row.addWidget(self._slew_btn)
        button_row.addWidget(self._aligned_btn)
        button_row.addStretch(1)
        button_row.addWidget(self._close_btn)

        layout = QtWidgets.QVBoxLayout(self)
        layout.addWidget(self._status)
        layout.addWidget(self._star_combo)
        layout.addLayout(button_row)

        self._slew_poll = QtCore.QTimer(self)
        self._slew_poll.setInterval(int(TIMEOUTS.telescope_poll_s * 1000))

        self._star_combo.currentIndexChanged.connect(self._on_star_changed)
        self._slew_btn.clicked.connect(self._on_slew)
        self._aligned_btn.clicked.connect(self._on_aligned)
        self._close_btn.clicked.connect(self._request_cancel)
        self._slew_poll.timeout.connect(self._poll_slew)
        self._solve_timeout_timer.timeout.connect(self._on_solve_timeout)

    @property
    def calibration_record(self) -> CalibrationRecord | None:
        return self._calibration_record

    def _selected_star(self):
        return self._star_combo.currentData()

    def _on_star_changed(self) -> None:
        self._slew_btn.setEnabled(self._selected_star() is not None)

    def _on_slew(self) -> None:
        star = self._selected_star()
        if star is None:
            return

        confirm = QtWidgets.QMessageBox.question(
            self,
            "Confirm Slew",
            f"Slew the main scope to {star.name}?\n\nYou can cancel now before motion starts.",
            QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.No,
            QtWidgets.QMessageBox.StandardButton.No,
        )
        if confirm != QtWidgets.QMessageBox.StandardButton.Yes:
            return

        target = Coordinates(ra_deg=star.ra_deg, dec_deg=star.dec_deg, epoch=self._epoch)
        try:
            self._telescope.slew_to_coordinates(target, timeout_s=TIMEOUTS.telescope_command_s)
        except Exception as exc:  # noqa: BLE001
            QtWidgets.QMessageBox.critical(self, "Slew Failed", str(exc))
            logger.exception("Slew failed")
            return

        self._slew_btn.setEnabled(False)
        self._aligned_btn.setEnabled(False)
        self._status.setText("Step 2: Slewing. Waiting for telescope to stop...")
        self._slew_poll.start()

    def _poll_slew(self) -> None:
        try:
            still_slewing = self._telescope.is_slewing()
        except Exception as exc:  # noqa: BLE001
            self._slew_poll.stop()
            QtWidgets.QMessageBox.critical(self, "Polling Error", str(exc))
            logger.exception("Slew polling failed")
            return

        if still_slewing:
            return

        self._slew_poll.stop()
        self._status.setText(
            "Step 3: Use eyepiece + hand paddle to center the selected star in the main telescope. "
            "Then press 'Star Is Aligned'."
        )
        self._aligned_btn.setEnabled(True)

    def _on_aligned(self) -> None:
        star = self._selected_star()
        if star is None:
            return
        self._solve_attempts = 0
        self._start_solve_attempt(star)

    def _request_cancel(self) -> None:
        if not self._solve_in_progress:
            self.reject()
            return

        self._cancel_requested = True
        self._solve_cancelled = True
        self._pending_close = True
        self._aligned_btn.setEnabled(False)
        self._slew_btn.setEnabled(False)
        self._close_btn.setEnabled(False)
        self._status.setText("Cancelling solve... please wait.")

    def _start_solve_attempt(self, star) -> None:
        if self._solve_in_progress:
            return

        if self._solve_attempts >= self._max_solve_attempts:
            QtWidgets.QMessageBox.warning(
                self,
                "Alignment Aborted",
                "Maximum solve attempts reached. Please try again later.",
            )
            self._aligned_btn.setEnabled(True)
            return

        self._solve_attempts += 1
        self._solve_cancelled = False
        self._solve_in_progress = True
        self._cancel_requested = False
        self._pending_retry = False
        self._aligned_btn.setEnabled(False)
        self._status.setText("Step 4: Settle, then Plate Solve...")
        QtWidgets.QApplication.processEvents(QtCore.QEventLoop.ProcessEventsFlag.AllEvents, 50)

        self._solve_thread = QtCore.QThread(self)
        self._solve_worker = _SolveWorker(self._telescope, self._solver, self._frame_provider, star)
        self._solve_worker.moveToThread(self._solve_thread)
        self._solve_thread.started.connect(self._solve_worker.run)
        self._solve_worker.solved.connect(self._on_solve_complete)
        self._solve_worker.failed.connect(self._on_solve_failed)
        self._solve_worker.progress.connect(self._on_solve_progress)
        self._solve_worker.finished.connect(self._on_solve_finished)
        self._solve_worker.finished.connect(self._solve_thread.quit)
        self._solve_worker.finished.connect(self._solve_worker.deleteLater)
        self._solve_thread.finished.connect(self._solve_thread.deleteLater)
        self._solve_thread.start()

        timeout_s = TIMEOUTS.camera_capture_s + TIMEOUTS.plate_solve_s + 10.0
        self._solve_timeout_timer.start(int(timeout_s * 1000))

    @QtCore.Slot(object, object, object)
    def _on_solve_complete(self, solve: object, mount: object, star: object) -> None:
        if self._solve_cancelled:
            return
        self._pending_retry = False
        if not isinstance(solve, SolveResult) or not isinstance(mount, Coordinates):
            self._handle_solve_error("Invalid solve response")
            return

        if not solve.success or solve.coordinates is None:
            self._handle_solve_error(solve.message or "Unknown solver error", retry_title="Plate Solve Failed")
            return

        finder = solve.coordinates
        offset_ra = wrap_ra_deg(mount.ra_deg - finder.ra_deg)
        offset_dec = mount.dec_deg - finder.dec_deg

        solved_minus_star_ra = wrap_ra_deg(finder.ra_deg - star.ra_deg)
        if solved_minus_star_ra > 180.0:
            solved_minus_star_ra -= 360.0
        solved_minus_star_dec = finder.dec_deg - star.dec_deg

        record = CalibrationRecord(
            timestamp_utc=now_utc_iso(),
            epoch=self._epoch,
            star_name=star.name,
            star_ra_deg=star.ra_deg,
            star_dec_deg=star.dec_deg,
            mount_ra_deg=mount.ra_deg,
            mount_dec_deg=mount.dec_deg,
            finder_ra_deg=finder.ra_deg,
            finder_dec_deg=finder.dec_deg,
            offset_ra_deg=offset_ra,
            offset_dec_deg=offset_dec,
            solve_confidence=solve.confidence,
        )
        self._calibration_record = record
        logger.info(
            "Calibration computed star=%s offset_ra=%.5f offset_dec=%.5f confidence=%s",
            record.star_name,
            record.offset_ra_deg,
            record.offset_dec_deg,
            record.solve_confidence,
        )

        metrics_text = format_plate_solve_metrics(solve.metrics)
        metrics_block = f"\n\n{metrics_text}" if metrics_text else ""

        QtWidgets.QMessageBox.information(
            self,
            "Calibration Saved",
            "Finder Calibration solve successful.\n\n"
            f"Solved coordinates:\n"
            f"RA {format_ra_deg_with_hms(finder.ra_deg, precision=6)}\n"
            f"Dec {format_dec_deg_with_dms(finder.dec_deg, precision=6)}\n"
            f"{self._format_altaz_text(finder.ra_deg, finder.dec_deg, precision=3)}\n\n"
            f"Solved minus star ({star.name}):\n"
            f"dRA {solved_minus_star_ra:.6f}°\n"
            f"dDec {solved_minus_star_dec:.6f}°\n\n"
            f"Calibration offset saved:\nRA offset: {offset_ra:.5f}°\nDec offset: {offset_dec:.5f}°"
            f"{metrics_block}",
        )
        self.accept()

    @QtCore.Slot(str)
    def _on_solve_failed(self, message: str) -> None:
        if self._solve_cancelled:
            return
        self._pending_retry = False
        self._handle_solve_error(message or "Unknown solver error")

    @QtCore.Slot(str)
    def _on_solve_progress(self, message: str) -> None:
        if self._solve_cancelled or self._cancel_requested:
            return
        if message:
            self._status.setText(f"Step 4: {message}")

    @QtCore.Slot()
    def _on_solve_finished(self) -> None:
        self._solve_in_progress = False
        self._solve_timeout_timer.stop()
        self._solve_worker = None
        self._solve_thread = None
        if self._pending_retry:
            self._pending_retry = False
            star = self._selected_star()
            if star is not None:
                self._start_solve_attempt(star)
                return
        if self._pending_close:
            self._pending_close = False
            self.reject()

    def _on_solve_timeout(self) -> None:
        if not self._solve_in_progress:
            return
        self._solve_cancelled = True
        if self._cancel_requested:
            return
        action = QtWidgets.QMessageBox.question(
            self,
            "Solve Timeout",
            "Alignment solve timed out. Try another image?",
            QtWidgets.QMessageBox.StandardButton.Retry | QtWidgets.QMessageBox.StandardButton.Cancel,
            QtWidgets.QMessageBox.StandardButton.Retry,
        )
        if action == QtWidgets.QMessageBox.StandardButton.Retry:
            star = self._selected_star()
            if star is not None:
                self._pending_retry = True
        else:
            self._aligned_btn.setEnabled(True)

    def _handle_solve_error(self, message: str, retry_title: str = "Calibration Solve Error") -> None:
        logger.warning("Alignment action failed: %s", message)
        if self._cancel_requested:
            return
        action = QtWidgets.QMessageBox.question(
            self,
            retry_title,
            f"{message}\n\nTry another image?",
            QtWidgets.QMessageBox.StandardButton.Retry | QtWidgets.QMessageBox.StandardButton.Cancel,
            QtWidgets.QMessageBox.StandardButton.Retry,
        )
        if action == QtWidgets.QMessageBox.StandardButton.Retry:
            self._status.setText("Step 4: Settle, then Plate Solve...")
            star = self._selected_star()
            if star is not None:
                self._start_solve_attempt(star)
            return
        self._aligned_btn.setEnabled(True)

    def closeEvent(self, event: QtGui.QCloseEvent) -> None:
        if self._solve_in_progress:
            self._request_cancel()
            event.ignore()
            return
        super().closeEvent(event)

    def _format_altaz_text(self, ra_deg: float, dec_deg: float, precision: int = 3) -> str:
        try:
            horizontal = radec_to_horizontal(
                ra_deg=ra_deg,
                dec_deg=dec_deg,
                observer_latitude_deg=self._observatory_latitude_deg,
                observer_longitude_deg=self._observatory_longitude_deg,
            )
            return format_horizontal_deg(horizontal, precision=precision)
        except Exception as exc:  # noqa: BLE001
            logger.debug("Failed Alt/Az conversion for ra=%.6f dec=%.6f: %s", ra_deg, dec_deg, exc)
            return "Alt/Az unavailable"
