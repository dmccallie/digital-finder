from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from alpaca import discovery, management
from alpaca.telescope import Telescope

from digital_finder.models import Coordinates
from digital_finder.services.interfaces import TelescopeClient

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DiscoveredTelescope:
    host: str
    port: int
    device_number: int
    server_name: str
    device_name: str

    @property
    def address(self) -> str:
        return f"{self.host}:{self.port}"

    @property
    def label(self) -> str:
        return f"{self.server_name} | {self.device_name} [{self.address}] #{self.device_number}"


def discover_alpaca_telescopes(numquery: int = 2, timeout_s: int = 2, trace: bool = False) -> list[DiscoveredTelescope]:
    """Discover Alpaca telescope devices on the local IPv4 network."""
    servers = discovery.search_ipv4(numquery=numquery, timeout=timeout_s, trace=trace)
    discovered: list[DiscoveredTelescope] = []

    for address in servers:
        try:
            description = management.description(address)
            server_name = description.get("ServerName", "Unknown Server")
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed reading Alpaca server description at %s: %s", address, exc)
            server_name = "Unknown Server"

        try:
            devices = management.configureddevices(address)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed reading configured devices at %s: %s", address, exc)
            continue

        host, _, port_text = address.partition(":")
        if not host or not port_text:
            continue

        try:
            port = int(port_text)
        except ValueError:
            continue

        for device in devices:
            if str(device.get("DeviceType", "")).lower() != "telescope":
                continue
            try:
                device_number = int(device.get("DeviceNumber", 0))
            except (TypeError, ValueError):
                device_number = 0
            device_name = str(device.get("DeviceName", f"Telescope {device_number}"))
            discovered.append(
                DiscoveredTelescope(
                    host=host,
                    port=port,
                    device_number=device_number,
                    server_name=server_name,
                    device_name=device_name,
                )
            )

    discovered.sort(key=lambda d: (d.host, d.port, d.device_number, d.device_name.lower()))
    return discovered


def _to_ra_hours(ra_deg: float) -> float:
    return (ra_deg % 360.0) / 15.0


def _to_ra_degrees(ra_hours: float) -> float:
    return (ra_hours % 24.0) * 15.0


class AlpacaTelescopeClient(TelescopeClient):
    """Alpyca-backed telescope client for ASCOM Alpaca endpoints."""

    def __init__(
        self,
        host: str,
        port: int,
        device_number: int = 0,
        epoch: str = "J2000",
        connect_timeout_s: float = 5.0,
    ) -> None:
        self.host = host
        self.port = port
        self.device_number = device_number
        self.epoch = epoch
        self._connect_timeout_s = max(0.5, float(connect_timeout_s))
        self._scope = Telescope(f"{self.host}:{self.port}", self.device_number)

    def _deadline(self, timeout_s: float) -> float:
        return time.monotonic() + max(0.1, timeout_s)

    def _connect_if_needed(self, timeout_s: float) -> None:
        if self._scope.Connected:
            return

        connect_budget_s = min(max(0.1, timeout_s), self._connect_timeout_s)
        deadline = self._deadline(connect_budget_s)
        self._scope.Connect()

        while self._scope.Connecting:
            if time.monotonic() >= deadline:
                raise TimeoutError("Timed out waiting for Alpaca telescope connection")
            time.sleep(0.1)

        if not self._scope.Connected:
            raise ConnectionError("Alpaca telescope did not report Connected=True")

    def connect(self, timeout_s: float) -> None:
        """Explicitly connect to the Alpaca telescope and wait for completion."""
        try:
            self._connect_if_needed(timeout_s=timeout_s)
        except Exception as exc:  # noqa: BLE001
            raise ConnectionError(
                f"Failed to connect to Alpaca telescope at {self.host}:{self.port} device #{self.device_number}: {exc}"
            ) from exc

        # Touch a read-only property to validate endpoint responsiveness post-connect.
        try:
            _ = self._scope.Name
        except Exception as exc:  # noqa: BLE001
            raise ConnectionError(
                f"Connected flag set but telescope endpoint {self.host}:{self.port} is not responding correctly: {exc}"
            ) from exc

    def _wait_until_not_slewing(self, timeout_s: float) -> None:
        deadline = self._deadline(timeout_s)
        while self._scope.Slewing:
            if time.monotonic() >= deadline:
                raise TimeoutError("Timed out waiting for Alpaca slew completion")
            time.sleep(0.5)

    def is_connected(self) -> bool:
        try:
            return bool(self._scope.Connected)
        except Exception:  # noqa: BLE001
            return False

    def slew_to_coordinates(self, target: Coordinates, timeout_s: float) -> None:
        self._connect_if_needed(timeout_s=timeout_s)
        try:
            self._scope.Tracking = True
        except Exception as exc:  # noqa: BLE001
            logger.warning("Unable to set Tracking=True before slew: %s", exc)
        self._scope.SlewToCoordinatesAsync(_to_ra_hours(target.ra_deg), target.dec_deg)

        # Confirm the async operation is healthy by touching Slewing once quickly.
        _ = self._scope.Slewing

    def is_slewing(self) -> bool:
        if not self.is_connected():
            return False
        try:
            return bool(self._scope.Slewing)
        except Exception:  # noqa: BLE001
            return False

    def get_coordinates(self, timeout_s: float) -> Coordinates:
        if not self.is_connected():
            raise ConnectionError(
                f"Telescope is not connected at {self.host}:{self.port} device #{self.device_number}"
            )
        deadline = self._deadline(timeout_s)

        while True:
            try:
                ra_hours = float(self._scope.RightAscension)
                dec_deg = float(self._scope.Declination)
                return Coordinates(ra_deg=_to_ra_degrees(ra_hours), dec_deg=dec_deg, epoch=self.epoch).normalized()
            except Exception:
                if time.monotonic() >= deadline:
                    raise TimeoutError("Timed out reading Alpaca coordinates")
                time.sleep(0.5)

    def sync_to_coordinates(self, target: Coordinates, timeout_s: float) -> None:
        self._connect_if_needed(timeout_s=timeout_s)
        self._scope.SyncToCoordinates(_to_ra_hours(target.ra_deg), target.dec_deg)

        # Ensure any previous async movement has settled after sync.
        self._wait_until_not_slewing(timeout_s=max(2.0, timeout_s))
