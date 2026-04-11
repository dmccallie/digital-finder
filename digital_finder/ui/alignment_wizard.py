from __future__ import annotations

import logging

from PySide6 import QtCore, QtWidgets

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
from digital_finder.stars import SAMPLE_CALIBRATION_STARS

logger = logging.getLogger(__name__)


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
        self._close_btn.clicked.connect(self.reject)
        self._slew_poll.timeout.connect(self._poll_slew)

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

        self._status.setText("Step 4: Settle, then Plate Solve...")
        self._aligned_btn.setEnabled(False)
        QtWidgets.QApplication.processEvents(QtCore.QEventLoop.ProcessEventsFlag.AllEvents, 50)

        while True:
            try:
                if self._telescope.is_slewing():
                    raise RuntimeError("Telescope is moving. Wait for motion to stop before solving.")

                mount = self._telescope.get_coordinates(timeout_s=TIMEOUTS.telescope_command_s)
                frame: Frame = self._frame_provider()
                if self._telescope.is_slewing():
                    raise RuntimeError("Telescope moved during capture. Please try again.")

                solve: SolveResult = self._solver.solve(frame, timeout_s=TIMEOUTS.plate_solve_s)
            except Exception as exc:  # noqa: BLE001
                logger.exception("Alignment action failed")
                action = QtWidgets.QMessageBox.question(
                    self,
                    "Calibration Solve Error",
                    f"{exc}\n\nTry another image?",
                    QtWidgets.QMessageBox.StandardButton.Retry | QtWidgets.QMessageBox.StandardButton.Cancel,
                    QtWidgets.QMessageBox.StandardButton.Retry,
                )
                if action == QtWidgets.QMessageBox.StandardButton.Retry:
                    self._status.setText("Step 4: Settle, then Plate Solve...")
                    continue
                self._aligned_btn.setEnabled(True)
                return

            if not solve.success or solve.coordinates is None:
                action = QtWidgets.QMessageBox.question(
                    self,
                    "Plate Solve Failed",
                    f"{solve.message or 'Unknown solver error'}\n\nTry another image?",
                    QtWidgets.QMessageBox.StandardButton.Retry | QtWidgets.QMessageBox.StandardButton.Cancel,
                    QtWidgets.QMessageBox.StandardButton.Retry,
                )
                if action == QtWidgets.QMessageBox.StandardButton.Retry:
                    self._status.setText("Step 4: Plate solving...")
                    continue
                self._aligned_btn.setEnabled(True)
                return

            finder = solve.coordinates
            break

        offset_ra = wrap_ra_deg(mount.ra_deg - finder.ra_deg)
        offset_dec = mount.dec_deg - finder.dec_deg

        # Solve-to-star deltas are useful for alignment diagnostics.
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
