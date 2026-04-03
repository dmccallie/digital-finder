from __future__ import annotations

from digital_finder.models import CalibrationStar


# Bright stars with decent seasonal spread; eastern options are noted to prefer pre-meridian alignment.
SAMPLE_CALIBRATION_STARS: list[CalibrationStar] = [
    CalibrationStar(name="Sirius", ra_deg=101.287155, dec_deg=-16.716116, notes="Winter, bright south"),
    CalibrationStar(name="Arcturus", ra_deg=213.915300, dec_deg=19.182409, notes="Spring, often east/south-east"),
    CalibrationStar(name="Vega", ra_deg=279.234734, dec_deg=38.783688, notes="Summer, high east in evening"),
    CalibrationStar(name="Altair", ra_deg=297.695827, dec_deg=8.868322, notes="Summer, east/south-east"),
    CalibrationStar(name="Fomalhaut", ra_deg=344.412750, dec_deg=-29.622236, notes="Autumn, south"),
    CalibrationStar(name="Capella", ra_deg=79.172327, dec_deg=45.997991, notes="Autumn/winter, north-east"),
    CalibrationStar(name="Betelgeuse", ra_deg=88.792939, dec_deg=7.407064, notes="Winter, east"),
]
