from __future__ import annotations

import json
import logging
from pathlib import Path

from platformdirs import user_data_dir

from digital_finder.config import APP_AUTHOR, APP_NAME
from digital_finder.models import CalibrationRecord

logger = logging.getLogger(__name__)


class CalibrationStore:
    def __init__(self) -> None:
        data_dir = Path(user_data_dir(APP_NAME, APP_AUTHOR))
        data_dir.mkdir(parents=True, exist_ok=True)
        self._path = data_dir / "calibration.json"

    @property
    def path(self) -> Path:
        return self._path

    def load_latest(self) -> CalibrationRecord | None:
        payload = self._load_payload()
        latest = payload.get("latest")
        if not latest:
            return None
        return CalibrationRecord.from_dict(latest)

    def save_new(self, record: CalibrationRecord) -> None:
        payload = self._load_payload()
        history = payload.setdefault("history", [])
        history.append(record.to_dict())
        payload["latest"] = record.to_dict()
        payload.setdefault("manual_invalidated", False)
        self._save_payload(payload)
        logger.info("Saved calibration for %s at %s", record.star_name, record.timestamp_utc)

    def set_manual_invalidated(self, invalidated: bool) -> None:
        payload = self._load_payload()
        payload["manual_invalidated"] = invalidated
        self._save_payload(payload)
        logger.info("Set manual_invalidated=%s", invalidated)

    def is_manual_invalidated(self) -> bool:
        payload = self._load_payload()
        return bool(payload.get("manual_invalidated", False))

    def _load_payload(self) -> dict:
        if not self._path.exists():
            return {"history": [], "latest": None, "manual_invalidated": False}
        with self._path.open("r", encoding="utf-8") as handle:
            return json.load(handle)

    def _save_payload(self, payload: dict) -> None:
        with self._path.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)
