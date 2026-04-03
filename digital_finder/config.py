from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Timeouts:
    telescope_command_s: float = 20.0
    telescope_poll_s: float = 0.5
    telescope_slew_max_s: float = 120.0
    camera_capture_s: float = 8.0
    plate_solve_s: float = 30.0


APP_NAME = "Digital Finder"
APP_AUTHOR = "Observatory"
DEFAULT_EPOCH = "J2000"

TIMEOUTS = Timeouts()
