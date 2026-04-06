from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Timeouts:
    telescope_connect_s: float = 5.0
    telescope_command_s: float = 20.0
    telescope_poll_s: float = 0.5
    telescope_slew_max_s: float = 120.0
    alignment_settle_s: float = 5.0
    camera_capture_s: float = 8.0
    plate_solve_s: float = 30.0


@dataclass(frozen=True)
class SolverConfig:
    astap_executable: str = r"C:\Program Files\astap\astap.exe"
    astap_test_image: str = "samples/rosette_1.fits"
    astap_downsample_factor: int = 2


APP_NAME = "Digital Finder"
APP_AUTHOR = "ASKC"
MAIN_SCOPE_NAME = "Ruisinger"
DEFAULT_EPOCH = "J2000"
DEFAULT_ALPACA_HOST = "127.0.0.1"
DEFAULT_ALPACA_PORT = 11111
DEFAULT_ALPACA_DEVICE_NUMBER = 0
ALPACA_DISCOVERY_NUMQUERY = 2
ALPACA_DISCOVERY_TIMEOUT_S = 2
KNOWN_ALPACA_TELESCOPES: tuple[tuple[str, str, int, int], ...] = (
    ("Device Hub Simulator", "127.0.0.1", 32323, 0),
    ("Device Hub Simulator (localhost)", "localhost", 32323, 0),
    ("ASCOM Remote Telescope (127.0.0.1)", "127.0.0.1", 11111, 0),
    ("ASCOM Remote Telescope (localhost)", "localhost", 11111, 0),
)

TIMEOUTS = Timeouts()
SOLVER_CONFIG = SolverConfig()
