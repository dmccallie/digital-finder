# Digital Finder - Project Context

## Purpose

Digital Finder is a Python Qt GUI app that uses a finder scope camera + plate solving to calibrate/sync a long focal length visual telescope.

Operational model:
- Finder scope: approximately 200 mm focal length, camera attached.
- Main scope: approximately 3500 mm focal length, visual use.
- Telescope control transport: ASCOM Alpaca (via alpyca), not COM.

Primary workflows:
1. Finder alignment (one-star calibration): compute and persist finder-to-main pointing offset.
2. Main scope calibration: plate-solve finder image, apply stored offset, send telescope sync.

## Implemented Architecture

Current code is organized into modular layers so hardware/solver backends are swappable.

### Entry points
- `main.py`: app launcher calling `digital_finder.app.run()`.
- `pyproject.toml`: project metadata, dependencies, and script entrypoint (`digital-finder`).

### Package layout
- `digital_finder/config.py`
  - App constants and timeout settings.
- `digital_finder/models.py`
  - Data models for coordinates, frames, solve results, calibration records.
  - Coordinate helpers (`wrap_ra_deg`, `clamp_dec_deg`).
- `digital_finder/stars.py`
  - Sample bright calibration stars list.
- `digital_finder/storage.py`
  - JSON persistence for calibration history/latest/manual invalidation.
- `digital_finder/logging_setup.py`
  - Rotating log file initialization via `platformdirs`.

Service abstractions and backends:
- `digital_finder/services/interfaces.py`
  - `TelescopeClient`, `CameraClient`, `PlateSolver` interfaces.
- `digital_finder/services/simulated.py`
  - Working simulator implementations:
    - `SimulatedTelescopeClient`
    - `SimulatedCameraClient`
    - `SimulatedPlateSolver`
- `digital_finder/services/alpaca_telescope.py`
  - Alpaca telescope scaffold (NotImplemented placeholders).
- `digital_finder/services/astap_solver.py`
  - ASTAP subprocess scaffold; invocation exists, output parsing pending.

UI:
- `digital_finder/app.py`
  - Main window/dashboard, live capture loop, controls, alignment trigger, calibration trigger.
- `digital_finder/ui/alignment_wizard.py`
  - Modal step-by-step finder alignment dialog.

## Implemented Features

1. Simulator mode (working end-to-end)
- Simulated telescope motion + polling.
- Simulated camera image generation.
- Simulated plate solve success with confidence score.

2. Alignment wizard flow
- Select star from picklist.
- Confirm slew dialog.
- Slew command + slew completion polling.
- Manual prompt to center star.
- "Star Is Aligned" action computes offset using plate-solved finder position and mount position.
- Calibration record persisted locally.

3. Main calibration flow
- Uses most recent frame (capture if needed).
- Solves on demand.
- Applies latest stored offset.
- Sends telescope sync command.

4. Calibration persistence
- JSON includes:
  - selected star, timestamp, epoch
  - mount and finder RA/Dec used for calibration
  - computed RA/Dec offsets
  - solve confidence
  - history + latest + manual invalidation flag

5. Dashboard status section
- Backend mode
- Telescope connected/slewing and coordinates
- Camera status
- Latest frame timestamp
- Last solve status/confidence
- Current calibration summary

6. Camera controls
- Exposure control (ms)
- Gain control
- Live loop start/stop
- Manual capture
- Display latest image with simple percentile stretch

7. Logging
- Key app events written to rotating log file.

8. Timeouts
- Centralized timeout values in config and used by telescope/camera/solver flows.

## Current Gaps / Known Stubs

1. Real Alpaca integration not yet wired
- `AlpacaTelescopeClient` methods are placeholders.
- Need concrete alpyca calls for connect/read RA/Dec/slew/poll/sync.

2. Real camera backend not yet wired
- Currently simulator camera is used.
- Need production camera implementation (e.g., ZWO ASI120MM SDK/backend).

3. ASTAP parsing not implemented
- ASTAP process launch scaffold exists.
- Need parser to extract solved RA/Dec and confidence/quality metric.

4. Epoch flexibility currently defaulting to J2000
- Structure supports epoch value, but transform logic is not yet implemented.

## Data and Logs

Platformdirs paths are used for user data and logs.
- Calibration JSON: user data dir (`calibration.json`).
- Log file: user log dir (`digital_finder.log`).

## Operational Decisions Recorded

- Keep one-star calibration model.
- Manual invalidation is supported; no automatic age expiry now.
- Track confidence score when solver provides one.
- Save key events and solutions in logs.
- Solve-on-demand preferred (not continuous plate solve).
- Keep backend interfaces isolated for swapping solver/camera/telescope implementations.

## Windows Deployment Notes

This project was initially developed in WSL/Linux and should be moved to native Windows for observatory deployment.

Recommended migration:
1. Commit and push code only.
2. Clone on Windows filesystem.
3. Recreate environment with uv on Windows:
   - `uv python install 3.12`
   - `uv venv`
   - `uv sync`
4. Run:
   - `uv run digital-finder`

Do not reuse/copy Linux `.venv` into Windows.

## Next High-Value Steps

1. Implement `AlpacaTelescopeClient` against your known working alpyca patterns.
2. Implement ASTAP result parsing in `AstapPlateSolver` and return solved coordinates + quality metric.
3. Add real camera backend module and switchable camera mode in UI settings.
4. Add a small runtime config file (or env var mapping) for Windows-specific endpoints and executable paths.
5. Add minimal integration tests for coordinate math and calibration serialization.
