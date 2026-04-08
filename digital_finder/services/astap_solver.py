from __future__ import annotations

import logging
import os
import re
import subprocess
import tempfile
import math
from typing import Callable

import numpy as np
from astropy.io import fits

from digital_finder.models import Coordinates, Frame, SolveMetrics, SolveResult
from digital_finder.services.interfaces import PlateSolver

logger = logging.getLogger(__name__)


class AstapPlateSolver(PlateSolver):
    """ASTAP subprocess wrapper.

    This scaffold writes the frame image to a temp FITS file per solve call.
    """

    def __init__(
        self,
        astap_executable: str = "astap.exe",
        downsample_factor: int = 2,
        approximate_coords_provider: Callable[[], Coordinates | None] | None = None,
    ) -> None:
        self.astap_executable = astap_executable
        self.downsample_factor = max(1, int(downsample_factor))
        self._approximate_coords_provider = approximate_coords_provider

    def _build_command(self, input_path: str, force_blind: bool = False) -> tuple[list[str], bool]:
        command = [self.astap_executable, "-f", input_path]

        if force_blind:
            # ASTAP treats radius > 180 as full-sky blind search.
            command.extend(["-r", "181"])
            return command, False

        hint: Coordinates | None = None
        if self._approximate_coords_provider is not None:
            try:
                hint = self._approximate_coords_provider()
            except Exception:  # noqa: BLE001
                hint = None

        if hint is not None:
            ra_hours = (hint.ra_deg % 360.0) / 15.0
            command.extend([
                "-ra",
                f"{ra_hours:.6f}",
                "-dec",
                f"{hint.dec_deg:.6f}",
                "-r",
                "10",
            ])
            return command, True
        else:
            # ASTAP treats radius > 180 as full-sky blind search.
            command.extend(["-r", "181"])
            return command, False

    def _log_astap_feedback(self, stage: str, completed: subprocess.CompletedProcess[str], input_path: str) -> None:
        stdout_text = (completed.stdout or "").strip()
        stderr_text = (completed.stderr or "").strip()
        sidecar_text = self._read_sidecar_text(input_path)

        logger.info("ASTAP %s return_code=%s", stage, completed.returncode)
        logger.info("ASTAP %s stdout:\n%s", stage, stdout_text if stdout_text else "<empty>")
        logger.info("ASTAP %s stderr:\n%s", stage, stderr_text if stderr_text else "<empty>")
        if sidecar_text:
            logger.info("ASTAP %s sidecar diagnostics:\n%s", stage, sidecar_text)

    def _summarize_output(self, stdout: str, stderr: str, max_chars: int = 400) -> str:
        text = (stderr or "").strip()
        if not text:
            text = (stdout or "").strip()
        if not text:
            return ""

        text = " ".join(text.split())
        if len(text) > max_chars:
            # Keep tail content because many solvers report specific reason near the end.
            return "..." + text[-(max_chars - 3) :]
        return text

    def _read_sidecar_text(self, input_path: str) -> str:
        sidecars = [
            os.path.splitext(input_path)[0] + ".ini",
            os.path.splitext(input_path)[0] + ".wcs",
            os.path.splitext(input_path)[0] + ".txt",
            os.path.splitext(input_path)[0] + ".log",
        ]
        parts: list[str] = []
        # Prefer lines that likely contain diagnostics to avoid noisy FITS headers.
        diag_line = re.compile(r"(error|fail|warning|star|solve|database|catalog|timeout|not found)", re.IGNORECASE)

        for sidecar in sidecars:
            if not os.path.exists(sidecar):
                continue
            try:
                with open(sidecar, "r", encoding="utf-8", errors="ignore") as handle:
                    for line in handle:
                        if diag_line.search(line):
                            parts.append(line.strip())
            except OSError:
                continue

        return "\n".join(parts)

    def _classify_failure(
        self,
        stdout: str,
        stderr: str,
        sidecar_text: str = "",
        returncode: int | None = None,
    ) -> str:
        haystack = f"{stderr}\n{stdout}\n{sidecar_text}".lower()

        reason = "ASTAP plate solve failed."
        tips: list[str] = []

        if "too few stars" in haystack or "not enough stars" in haystack:
            reason = "ASTAP failed: too few stars detected."
            tips = [
                "Increase exposure time and/or gain.",
                "Improve focus and avoid cloud/haze.",
                "Reduce downsampling so fainter stars remain detectable.",
            ]
        elif "no stars" in haystack or "0 stars" in haystack:
            reason = "ASTAP failed: no stars detected in the image."
            tips = [
                "Check camera exposure/gain and lens cap/filter state.",
                "Verify image stretch is not clipping stars.",
            ]
        elif "not solved" in haystack or "no solution" in haystack or "failed to solve" in haystack:
            reason = "ASTAP could not find a valid star-pattern match."
            tips = [
                "Increase star count (exposure/gain) and ensure stars are round.",
                "Verify approximate focal length/pixel scale settings in ASTAP if configured.",
            ]
        elif "database" in haystack and ("not found" in haystack or "missing" in haystack):
            reason = "ASTAP star database appears missing or not configured."
            tips = [
                "Install/configure ASTAP star database files.",
                "Verify ASTAP can solve images when run manually.",
            ]
        elif "cannot open" in haystack or "file not found" in haystack:
            reason = "ASTAP could not open the input image file."
            tips = [
                "Verify the temp/source image path is accessible.",
                "Check file permissions and disk availability.",
            ]

        output_summary = self._summarize_output(stdout, stderr)
        sidecar_summary = " ".join(sidecar_text.split())
        if len(sidecar_summary) > 280:
            sidecar_summary = sidecar_summary[:277] + "..."

        msg = reason
        if tips:
            msg += "\nSuggestions:\n- " + "\n- ".join(tips)
        if sidecar_summary:
            msg += f"\nASTAP sidecar: {sidecar_summary}"
        if output_summary:
            msg += f"\nASTAP output: {output_summary}"
        elif returncode is not None:
            msg += f"\nExit code: {returncode}"
        return msg

    def _write_temp_image(self, image: np.ndarray) -> str:
        # Write a temporary downsized FITS image for ASTAP.
        if image.ndim != 2:
            raise ValueError("ASTAP requires a 2D monochrome image")

        arr = np.asarray(image)
        if self.downsample_factor > 1:
            arr = arr[:: self.downsample_factor, :: self.downsample_factor]

        if np.issubdtype(arr.dtype, np.integer):
            arr = arr.astype(np.int32, copy=False)
        else:
            arr = arr.astype(np.float32, copy=False)

        with tempfile.NamedTemporaryFile(prefix="digital_finder_", suffix=".fits", delete=False) as tmp:
            tmp_path = tmp.name

        fits.PrimaryHDU(arr).writeto(tmp_path, overwrite=True)
        return tmp_path

    def _parse_solution_coordinates(self, input_path: str) -> Coordinates | None:
        # ASTAP writes a sidecar INI (and WCS) containing CRVAL1/CRVAL2 in degrees.
        sidecars = [
            os.path.splitext(input_path)[0] + ".ini",
            os.path.splitext(input_path)[0] + ".wcs",
        ]
        pattern = re.compile(r"^\s*(CRVAL1|CRVAL2)\s*=\s*([^\s/]+)")

        for sidecar in sidecars:
            if not os.path.exists(sidecar):
                continue

            ra_deg: float | None = None
            dec_deg: float | None = None
            try:
                with open(sidecar, "r", encoding="utf-8", errors="ignore") as handle:
                    for line in handle:
                        match = pattern.match(line)
                        if not match:
                            continue
                        key, value = match.groups()
                        try:
                            numeric = float(value.replace("D", "E"))
                        except ValueError:
                            continue
                        if key == "CRVAL1":
                            ra_deg = numeric
                        elif key == "CRVAL2":
                            dec_deg = numeric

                if ra_deg is not None and dec_deg is not None:
                    return Coordinates(ra_deg=ra_deg, dec_deg=dec_deg, epoch="J2000").normalized()
            except OSError:
                continue

        return None

    def _read_sidecar_keywords(self, input_path: str) -> dict[str, float]:
        sidecars = [
            os.path.splitext(input_path)[0] + ".wcs",
            os.path.splitext(input_path)[0] + ".ini",
        ]
        keywords: dict[str, float] = {}

        # Handles lines like: KEY = value / comment
        pattern = re.compile(r"^\s*([A-Za-z0-9_]+)\s*=\s*([^/\n\r]+)")

        for sidecar in sidecars:
            if not os.path.exists(sidecar):
                continue
            try:
                with open(sidecar, "r", encoding="utf-8", errors="ignore") as handle:
                    for line in handle:
                        match = pattern.match(line)
                        if not match:
                            continue
                        key, raw_value = match.groups()
                        key = key.strip().upper()
                        value_text = raw_value.strip().replace("D", "E")
                        try:
                            keywords[key] = float(value_text)
                        except ValueError:
                            continue
            except OSError:
                continue

        return keywords

    def _extract_solve_metrics(self, input_path: str, frame: Frame) -> SolveMetrics | None:
        kw = self._read_sidecar_keywords(input_path)
        if not kw:
            return None

        naxis1 = int(kw["NAXIS1"]) if "NAXIS1" in kw else None
        naxis2 = int(kw["NAXIS2"]) if "NAXIS2" in kw else None
        if (naxis1 is None or naxis2 is None) and isinstance(frame.data, np.ndarray) and frame.data.ndim == 2:
            naxis2 = frame.data.shape[0]
            naxis1 = frame.data.shape[1]

        cd11 = kw.get("CD1_1")
        cd12 = kw.get("CD1_2")
        cd21 = kw.get("CD2_1")
        cd22 = kw.get("CD2_2")

        cdelt1 = kw.get("CDELT1")
        cdelt2 = kw.get("CDELT2")

        if (cd11 is None or cd12 is None or cd21 is None or cd22 is None) and cdelt1 is not None and cdelt2 is not None:
            pc11 = kw.get("PC1_1", 1.0)
            pc12 = kw.get("PC1_2", 0.0)
            pc21 = kw.get("PC2_1", 0.0)
            pc22 = kw.get("PC2_2", 1.0)
            cd11 = cdelt1 * pc11
            cd12 = cdelt1 * pc12
            cd21 = cdelt2 * pc21
            cd22 = cdelt2 * pc22

        scale_x_deg_per_px: float | None = None
        scale_y_deg_per_px: float | None = None
        if cd11 is not None and cd12 is not None and cd21 is not None and cd22 is not None:
            # CD matrix column norms give local degree-per-pixel scale on each image axis.
            scale_x_deg_per_px = math.sqrt(cd11 * cd11 + cd21 * cd21)
            scale_y_deg_per_px = math.sqrt(cd12 * cd12 + cd22 * cd22)
        elif cdelt1 is not None and cdelt2 is not None:
            scale_x_deg_per_px = abs(cdelt1)
            scale_y_deg_per_px = abs(cdelt2)

        image_scale_arcsec_per_px: float | None = None
        if scale_x_deg_per_px is not None and scale_y_deg_per_px is not None:
            image_scale_arcsec_per_px = ((scale_x_deg_per_px + scale_y_deg_per_px) / 2.0) * 3600.0

        rotation_deg: float | None = None
        if "CROTA2" in kw:
            rotation_deg = kw["CROTA2"]
        elif cd12 is not None and cd22 is not None:
            # Orientation estimate: angle east of celestial north in image coordinates.
            rotation_deg = math.degrees(math.atan2(cd12, cd22))

        if rotation_deg is not None:
            while rotation_deg > 180.0:
                rotation_deg -= 360.0
            while rotation_deg <= -180.0:
                rotation_deg += 360.0

        fov_width_deg: float | None = None
        fov_height_deg: float | None = None
        if naxis1 is not None and naxis2 is not None and scale_x_deg_per_px is not None and scale_y_deg_per_px is not None:
            fov_width_deg = naxis1 * scale_x_deg_per_px
            fov_height_deg = naxis2 * scale_y_deg_per_px

        if (
            image_scale_arcsec_per_px is None
            and rotation_deg is None
            and fov_width_deg is None
            and fov_height_deg is None
        ):
            return None

        return SolveMetrics(
            image_scale_arcsec_per_px=image_scale_arcsec_per_px,
            rotation_deg=rotation_deg,
            fov_width_deg=fov_width_deg,
            fov_height_deg=fov_height_deg,
        )

    def solve(self, frame: Frame, timeout_s: float) -> SolveResult:
        temp_image_path: str | None = None
        input_path: str | None = None

        if isinstance(frame.data, np.ndarray):
            try:
                temp_image_path = self._write_temp_image(frame.data)
                input_path = temp_image_path
            except Exception:
                input_path = None

        if input_path is None and frame.source_path is not None and os.path.exists(frame.source_path):
            input_path = frame.source_path

        if input_path is None:
            return SolveResult(success=False, message="ASTAP solver requires frame.data array or valid frame.source_path")

        try:
            command, used_hint = self._build_command(input_path)
            logger.info("Running ASTAP command: %s", subprocess.list2cmdline(command))
            try:
                completed = subprocess.run(command, capture_output=True, text=True, timeout=timeout_s, check=False)
            except subprocess.TimeoutExpired:
                return SolveResult(success=False, message="ASTAP timed out")
            except FileNotFoundError:
                return SolveResult(
                    success=False,
                    message=(
                        f"ASTAP executable not found: {self.astap_executable}\n"
                        "Set a valid ASTAP executable path in configuration."
                    ),
                )
            except OSError as exc:
                return SolveResult(success=False, message=f"Failed to start ASTAP: {exc}")

            self._log_astap_feedback("primary", completed, input_path)

            coordinates = self._parse_solution_coordinates(input_path) if completed.returncode == 0 else None
            needs_blind_retry = used_hint and (completed.returncode != 0 or coordinates is None)

            if needs_blind_retry:
                blind_command, _ = self._build_command(input_path, force_blind=True)
                logger.info(
                    "ASTAP hinted solve failed; retrying blind search command: %s",
                    subprocess.list2cmdline(blind_command),
                )
                try:
                    blind_completed = subprocess.run(
                        blind_command,
                        capture_output=True,
                        text=True,
                        timeout=timeout_s,
                        check=False,
                    )
                except subprocess.TimeoutExpired:
                    return SolveResult(success=False, message="ASTAP timed out")
                except OSError as exc:
                    return SolveResult(success=False, message=f"Failed to start ASTAP blind retry: {exc}")

                completed = blind_completed
                self._log_astap_feedback("blind-retry", completed, input_path)
                coordinates = self._parse_solution_coordinates(input_path) if completed.returncode == 0 else None

            if completed.returncode != 0:
                sidecar_text = self._read_sidecar_text(input_path)
                msg = self._classify_failure(
                    completed.stdout,
                    completed.stderr,
                    sidecar_text=sidecar_text,
                    returncode=completed.returncode,
                )
                return SolveResult(success=False, message=msg)

            if coordinates is None:
                sidecar_text = self._read_sidecar_text(input_path)
                summary = self._classify_failure(
                    completed.stdout,
                    completed.stderr,
                    sidecar_text=sidecar_text,
                    returncode=completed.returncode,
                )
                if "ASTAP plate solve failed." in summary:
                    summary = (
                        "ASTAP completed but solved RA/Dec were not found in sidecar output.\n"
                        f"{summary}"
                    )
                return SolveResult(success=False, message=summary)

            metrics = self._extract_solve_metrics(input_path, frame)

            return SolveResult(
                success=True,
                coordinates=coordinates,
                confidence=None,
                metrics=metrics,
                message=completed.stdout.strip() or "ASTAP solve successful",
            )
        finally:
            try:
                if temp_image_path and os.path.exists(temp_image_path):
                    os.remove(temp_image_path)
            except OSError:
                pass
