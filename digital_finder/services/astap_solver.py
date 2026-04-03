from __future__ import annotations

import os
import subprocess

from digital_finder.models import Frame, SolveResult
from digital_finder.services.interfaces import PlateSolver


class AstapPlateSolver(PlateSolver):
    """ASTAP subprocess wrapper.

    This scaffold intentionally requires an image file path in frame.source_path.
    """

    def __init__(self, astap_executable: str = "astap.exe") -> None:
        self.astap_executable = astap_executable

    def solve(self, frame: Frame, timeout_s: float) -> SolveResult:
        if frame.source_path is None:
            return SolveResult(success=False, message="ASTAP solver requires frame.source_path")
        if not os.path.exists(frame.source_path):
            return SolveResult(success=False, message="Frame image path not found")

        command = [self.astap_executable, "-f", frame.source_path, "-r", "10"]
        try:
            completed = subprocess.run(command, capture_output=True, text=True, timeout=timeout_s, check=False)
        except subprocess.TimeoutExpired:
            return SolveResult(success=False, message="ASTAP timed out")

        if completed.returncode != 0:
            msg = completed.stderr.strip() or completed.stdout.strip() or "ASTAP failed"
            return SolveResult(success=False, message=msg)

        return SolveResult(success=False, message="ASTAP output parsing not implemented yet")
