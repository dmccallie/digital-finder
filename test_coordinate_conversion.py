from __future__ import annotations

import unittest
from unittest.mock import patch

from astropy.time import Time as AstropyTime

from digital_finder.models import Coordinates, convert_coordinates_epoch, normalize_epoch_name
from digital_finder.services.alpaca_telescope import AlpacaTelescopeClient


class _FakeScope:
    def __init__(self, _address: str, _device_number: int) -> None:
        self.Connected = False
        self.Connecting = False
        self.Name = "Fake Scope"
        self.Tracking = False
        self.Slewing = False
        self.RightAscension = 0.0
        self.Declination = 0.0
        self.last_slew: tuple[float, float] | None = None
        self.last_sync: tuple[float, float] | None = None

    def Connect(self) -> None:
        self.Connected = True
        self.Connecting = False

    def SlewToCoordinatesAsync(self, ra_hours: float, dec_deg: float) -> None:
        self.last_slew = (ra_hours, dec_deg)

    def SyncToCoordinates(self, ra_hours: float, dec_deg: float) -> None:
        self.last_sync = (ra_hours, dec_deg)


class CoordinateConversionTests(unittest.TestCase):
    def test_normalize_epoch_name_defaults_to_j2000(self) -> None:
        self.assertEqual(normalize_epoch_name("jnow"), "JNOW")
        self.assertEqual(normalize_epoch_name("unexpected"), "J2000")

    def test_convert_coordinates_epoch_round_trips_between_j2000_and_jnow(self) -> None:
        observation_time = AstropyTime("2026-05-09T00:00:00", scale="utc")
        arcturus = Coordinates(ra_deg=213.915300, dec_deg=19.182409, epoch="J2000")

        jnow = convert_coordinates_epoch(arcturus, "JNOW", observation_time=observation_time)
        round_trip = convert_coordinates_epoch(jnow, "J2000", observation_time=observation_time)

        self.assertGreater(abs(jnow.ra_deg - arcturus.ra_deg), 0.2)
        self.assertGreater(abs(jnow.dec_deg - arcturus.dec_deg), 0.05)
        self.assertAlmostEqual(round_trip.ra_deg, arcturus.ra_deg, places=6)
        self.assertAlmostEqual(round_trip.dec_deg, arcturus.dec_deg, places=6)

    @patch("digital_finder.services.alpaca_telescope.Telescope", _FakeScope)
    @patch("digital_finder.models.AstropyTime.now", return_value=AstropyTime("2026-05-09T00:00:00", scale="utc"))
    def test_alpaca_client_converts_between_internal_j2000_and_telescope_jnow(self, _mock_time_now) -> None:
        client = AlpacaTelescopeClient(
            host="127.0.0.1",
            port=11111,
            device_number=0,
            epoch="J2000",
            converse_with_telescope_in_jnow=True,
        )
        client.connect(timeout_s=1.0)

        internal_target = Coordinates(ra_deg=213.915300, dec_deg=19.182409, epoch="J2000")
        expected_telescope_target = convert_coordinates_epoch(
            internal_target,
            "JNOW",
            observation_time=AstropyTime("2026-05-09T00:00:00", scale="utc"),
        )

        client.slew_to_coordinates(internal_target, timeout_s=1.0)
        assert isinstance(client._scope, _FakeScope)
        assert client._scope.last_slew is not None
        self.assertAlmostEqual(client._scope.last_slew[0] * 15.0, expected_telescope_target.ra_deg, places=6)
        self.assertAlmostEqual(client._scope.last_slew[1], expected_telescope_target.dec_deg, places=6)

        client._scope.RightAscension = expected_telescope_target.ra_deg / 15.0
        client._scope.Declination = expected_telescope_target.dec_deg
        reported = client.get_coordinates(timeout_s=1.0)

        self.assertEqual(reported.epoch, "J2000")
        self.assertAlmostEqual(reported.ra_deg, internal_target.ra_deg, places=6)
        self.assertAlmostEqual(reported.dec_deg, internal_target.dec_deg, places=6)

    def test_zero_offset_fallback_target_matches_solved_coordinates(self) -> None:
        solved = Coordinates(ra_deg=123.456789, dec_deg=-22.334455, epoch="J2000")

        target = Coordinates(ra_deg=solved.ra_deg, dec_deg=solved.dec_deg, epoch="J2000").normalized()

        self.assertAlmostEqual(target.ra_deg, solved.ra_deg, places=6)
        self.assertAlmostEqual(target.dec_deg, solved.dec_deg, places=6)