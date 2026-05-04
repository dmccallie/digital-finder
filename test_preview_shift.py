from datetime import datetime, timedelta, timezone

import unittest

import numpy as np

from digital_finder.models import (
    CalibrationRecord,
    Coordinates,
    Frame,
    PreviewWcsReference,
    SolveMetrics,
    calibration_preview_source_offset_px,
    preview_wcs_reference_is_valid,
    shift_preview_image,
)
from digital_finder.services.simulated import SimulatedPlateSolver


class PreviewShiftTests(unittest.TestCase):
    def test_calibration_preview_source_offset_uses_projected_ra(self) -> None:
        calibration = CalibrationRecord(
            timestamp_utc="2026-05-03T00:00:00+00:00",
            epoch="J2000",
            star_name="Test",
            star_ra_deg=0.0,
            star_dec_deg=0.0,
            mount_ra_deg=0.0,
            mount_dec_deg=0.0,
            finder_ra_deg=0.0,
            finder_dec_deg=0.0,
            offset_ra_deg=2.0,
            offset_dec_deg=1.0,
        )
        solve_coordinates = Coordinates(ra_deg=120.0, dec_deg=60.0)
        metrics = SolveMetrics(
            cd1_1=0.01,
            cd1_2=0.0,
            cd2_1=0.0,
            cd2_2=0.02,
        )

        offset = calibration_preview_source_offset_px(solve_coordinates, metrics, calibration)

        self.assertIsNotNone(offset)
        assert offset is not None
        self.assertAlmostEqual(offset[0], 100.0)
        self.assertAlmostEqual(offset[1], 50.0)

    def test_shift_preview_image_fills_exposed_edges_with_gray(self) -> None:
        image = np.array(
            [
                [0, 1, 2, 3],
                [4, 5, 6, 7],
                [8, 9, 10, 11],
            ],
            dtype=np.uint8,
        )

        shifted = shift_preview_image(image, source_offset_x_px=1.0, source_offset_y_px=-1.0, fill_value=128)

        expected = np.array(
            [
                [128, 128, 128, 128],
                [1, 2, 3, 128],
                [5, 6, 7, 128],
            ],
            dtype=np.uint8,
        )
        np.testing.assert_array_equal(shifted, expected)

    def test_preview_wcs_reference_is_valid_for_small_tracking_motion(self) -> None:
        reference = PreviewWcsReference(
            coordinates=Coordinates(ra_deg=100.0, dec_deg=20.0),
            metrics=SolveMetrics(cd1_1=-0.01, cd1_2=0.0, cd2_1=0.0, cd2_2=0.01),
            captured_at_utc=(datetime.now(tz=timezone.utc) - timedelta(seconds=120)).isoformat(),
        )

        is_valid = preview_wcs_reference_is_valid(
            reference,
            Coordinates(ra_deg=102.0, dec_deg=21.0),
            max_age_s=600.0,
            max_ra_shift_deg=5.0,
            max_dec_shift_deg=5.0,
        )

        self.assertTrue(is_valid)

    def test_preview_wcs_reference_invalidates_on_large_mount_move(self) -> None:
        reference = PreviewWcsReference(
            coordinates=Coordinates(ra_deg=100.0, dec_deg=20.0),
            metrics=SolveMetrics(cd1_1=-0.01, cd1_2=0.0, cd2_1=0.0, cd2_2=0.01),
            captured_at_utc=(datetime.now(tz=timezone.utc) - timedelta(seconds=120)).isoformat(),
        )

        is_valid = preview_wcs_reference_is_valid(
            reference,
            Coordinates(ra_deg=107.0, dec_deg=20.0),
            max_age_s=600.0,
            max_ra_shift_deg=5.0,
            max_dec_shift_deg=5.0,
        )

        self.assertFalse(is_valid)

    def test_simulated_plate_solver_returns_preview_metrics(self) -> None:
        solver = SimulatedPlateSolver()
        frame = Frame(
            data=np.zeros((480, 640), dtype=np.uint16),
            captured_at_utc=datetime.now(tz=timezone.utc).isoformat(),
            true_coords=Coordinates(ra_deg=150.0, dec_deg=30.0),
        )

        result = solver.solve(frame, timeout_s=5.0)

        self.assertTrue(result.success)
        self.assertIsNotNone(result.metrics)
        assert result.metrics is not None
        self.assertIsNotNone(result.metrics.cd1_1)
        self.assertIsNotNone(result.metrics.cd2_2)


if __name__ == "__main__":
    unittest.main()