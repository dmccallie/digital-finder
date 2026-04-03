# Digital Finder (Telescope Alignment Assistant)

Python + Qt application for using a finder scope + camera as a digital alignment reference for a long focal-length visual telescope.

## Current Status

- GUI scaffold implemented with PySide6.
- Full simulator mode implemented end-to-end:
	- Simulated telescope
	- Simulated camera
	- Simulated plate solver
- Finder alignment workflow implemented as a modal step-by-step wizard.
- Calibration persistence implemented (JSON) with history + latest + manual invalidation.
- Main telescope calibration flow implemented (solve finder frame -> apply offset -> sync telescope).
- Event logging to rotating file implemented.

## Design Highlights

- Modular service interfaces for easy backend swapping:
	- `TelescopeClient`
	- `CameraClient`
	- `PlateSolver`
- Backend stubs included for:
	- Alpaca telescope via alpyca (to be wired to local environment)
	- ASTAP subprocess solver (command invocation scaffolded; parser pending)
- Internal coordinate convention is degrees with RA wrap handling and Dec clamping.
- Epoch is configurable (default J2000).

## Workflow

### Finder Alignment

1. Pick bright star.
2. Confirm and execute slew.
3. Wait for slew completion (poll).
4. Manually center star in main scope.
5. Press "Star Is Aligned".
6. Read mount coordinates + plate solve finder frame.
7. Compute offset and persist calibration.

### Calibrate Main Telescope

1. Capture/use most recent finder frame.
2. Plate solve on demand.
3. Apply most recent saved offset.
4. Send Alpaca sync command.

## Platform Notes

- Target OS: Windows 10.
- Simulator mode currently provides complete in-app testing without hardware.
- Real Alpaca + ASTAP integrations are intentionally isolated and can be completed without GUI rewrites.

## Run

```bash
uv sync
uv run digital-finder
```

or

```bash
python main.py
```

## Data + Logs

- Calibration JSON path: user data directory via `platformdirs`.
- Log file path: user log directory via `platformdirs`.

Both locations are platform-specific and suitable for Windows deployment.
