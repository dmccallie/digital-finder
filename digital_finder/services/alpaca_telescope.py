from __future__ import annotations

from digital_finder.models import Coordinates
from digital_finder.services.interfaces import TelescopeClient


class AlpacaTelescopeClient(TelescopeClient):
    """Placeholder for a real alpyca-backed telescope client.

    Wire this class to the version of alpyca in use at your observatory.
    """

    def __init__(self, host: str, port: int, device_number: int = 0, epoch: str = "J2000") -> None:
        self.host = host
        self.port = port
        self.device_number = device_number
        self.epoch = epoch

    def is_connected(self) -> bool:
        raise NotImplementedError("Implement alpyca telescope connectivity for your environment")

    def slew_to_coordinates(self, target: Coordinates, timeout_s: float) -> None:
        raise NotImplementedError("Implement alpyca SlewToCoordinates call")

    def is_slewing(self) -> bool:
        raise NotImplementedError("Implement alpyca slew polling")

    def get_coordinates(self, timeout_s: float) -> Coordinates:
        raise NotImplementedError("Implement alpyca RightAscension/Declination read")

    def sync_to_coordinates(self, target: Coordinates, timeout_s: float) -> None:
        raise NotImplementedError("Implement alpyca SyncToCoordinates call")
