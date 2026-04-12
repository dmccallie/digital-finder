from __future__ import annotations

from digital_finder.models import CalibrationStar


# Bright stars with decent seasonal spread; eastern options are noted to prefer pre-meridian alignment.
SAMPLE_CALIBRATION_STARS: list[CalibrationStar] = [
    
    CalibrationStar(name="HD 46223", ra_deg=98.041667, dec_deg=5.005833, notes="Rosette central star test target"),

    CalibrationStar(name="Arcturus",    ra_deg=213.915300, dec_deg=19.182409,  notes="Spring, often east/south-east"),
    CalibrationStar(name="Pollux",      ra_deg=116.328958, dec_deg=28.026199,  notes="Spring, east/south-east"),
    CalibrationStar(name="Spica",       ra_deg=201.298247, dec_deg=-11.161319, notes="Spring, south/south-east"),
    CalibrationStar(name="Alphard",     ra_deg=142.27    , dec_deg=-8.658603,  notes="Spring/summer, south/south-east"),

    CalibrationStar(name="Regulus",     ra_deg=152.092962, dec_deg=11.967208,  notes="Spring/summer, east/south-east"),
    CalibrationStar(name="Vega",        ra_deg=279.234735, dec_deg=38.783689,  notes="Summer, high east in evening"),
    CalibrationStar(name="Altair",      ra_deg=297.695827, dec_deg=8.868321,   notes="Summer, east/south-east"),
    CalibrationStar(name="Antares",     ra_deg=247.351915, dec_deg=-26.432003, notes="Summer, south/south-west"),
    CalibrationStar(name="Deneb",       ra_deg=310.357980, dec_deg=45.280339,  notes="Summer/autumn, high north-east"),
    
    CalibrationStar(name="Fomalhaut",   ra_deg=344.412693, dec_deg=-29.622236, notes="Autumn, south"),
    CalibrationStar(name="Capella",     ra_deg=79.172328,  dec_deg=45.997991,  notes="Autumn/winter, north-east"),
    CalibrationStar(name="Alpheratz",   ra_deg=2.096542,   dec_deg=29.090833,  notes="Autumn/winter, east/north-east"),
    CalibrationStar(name="Aldebaran",   ra_deg=68.980163,  dec_deg=16.509302,  notes="Autumn/winter, east/north-east"),

    CalibrationStar(name="Sirius",      ra_deg=101.287155, dec_deg=-16.716116, notes="Winter, bright south"),
    CalibrationStar(name="Betelgeuse",  ra_deg=88.792939,  dec_deg=7.407064,   notes="Winter, east"),
    CalibrationStar(name="Procyon",     ra_deg=114.825498, dec_deg=5.224988,   notes="Winter, east"),
    CalibrationStar(name="Bellatrix",   ra_deg=81.282763,  dec_deg=6.349703,   notes="Winter, east"),

]
