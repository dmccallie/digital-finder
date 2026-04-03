from __future__ import annotations

from abc import ABC, abstractmethod

from digital_finder.models import Coordinates, Frame, SolveResult


class TelescopeClient(ABC):
    @abstractmethod
    def is_connected(self) -> bool:
        raise NotImplementedError

    @abstractmethod
    def slew_to_coordinates(self, target: Coordinates, timeout_s: float) -> None:
        raise NotImplementedError

    @abstractmethod
    def is_slewing(self) -> bool:
        raise NotImplementedError

    @abstractmethod
    def get_coordinates(self, timeout_s: float) -> Coordinates:
        raise NotImplementedError

    @abstractmethod
    def sync_to_coordinates(self, target: Coordinates, timeout_s: float) -> None:
        raise NotImplementedError


class CameraClient(ABC):
    @abstractmethod
    def is_connected(self) -> bool:
        raise NotImplementedError

    @abstractmethod
    def set_exposure_ms(self, exposure_ms: int) -> None:
        raise NotImplementedError

    @abstractmethod
    def set_gain(self, gain: int) -> None:
        raise NotImplementedError

    @abstractmethod
    def capture_frame(self, timeout_s: float) -> Frame:
        raise NotImplementedError


class PlateSolver(ABC):
    @abstractmethod
    def solve(self, frame: Frame, timeout_s: float) -> SolveResult:
        raise NotImplementedError
