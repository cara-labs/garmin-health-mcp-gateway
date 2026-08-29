from __future__ import annotations

import io
import logging
import os
import time
import zipfile
from abc import ABC, abstractmethod
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class GarminProvider(ABC):
    """Replaceable boundary around the unofficial Garmin Connect client."""

    @abstractmethod
    def authenticate(self, *, interactive: bool = False) -> None: ...

    @abstractmethod
    def get_daily_summary(self, target: date) -> dict[str, Any]: ...

    @abstractmethod
    def get_sleep(self, target: date) -> dict[str, Any]: ...

    @abstractmethod
    def get_hrv(self, target: date) -> dict[str, Any] | None: ...

    @abstractmethod
    def get_body_battery(self, target: date) -> Any: ...

    @abstractmethod
    def get_stress(self, target: date) -> dict[str, Any]: ...

    @abstractmethod
    def get_training_readiness(self, target: date) -> Any: ...

    @abstractmethod
    def get_training_status(self, target: date) -> dict[str, Any]: ...

    @abstractmethod
    def get_vo2max(self, target: date) -> dict[str, Any]: ...

    @abstractmethod
    def get_fitness_age(self, target: date) -> dict[str, Any]: ...

    @abstractmethod
    def get_lactate_threshold(self, target: date) -> Any: ...

    @abstractmethod
    def get_activities(self, start_date: date, end_date: date) -> list[dict[str, Any]]: ...

    @abstractmethod
    def get_activity(self, activity_id: int) -> dict[str, Any]: ...

    @abstractmethod
    def download_fit(self, activity_id: int) -> bytes: ...


class GarminConnectProvider(GarminProvider):
    def __init__(
        self,
        *,
        email: str | None,
        password: str | None,
        token_store: Path,
        request_delay_seconds: float = 1.25,
    ) -> None:
        self.email = email
        self.password = password
        self.token_store = token_store
        self.request_delay_seconds = request_delay_seconds
        self._client: Any = None

    def authenticate(self, *, interactive: bool = False) -> None:
        from garminconnect import Garmin

        self.token_store.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.token_store, 0o700)
        token_file = self.token_store / "garmin_tokens.json"
        if not token_file.exists() and (not self.email or not self.password):
            raise RuntimeError(
                "No Garmin session exists. Provide Garmin credential secrets and run "
                "`docker compose run --rm collector auth` once."
            )

        prompt: Callable[[], str] | None = None
        if interactive:

            def prompt() -> str:
                return input("Garmin MFA code: ").strip()

        self._client = Garmin(
            self.email,
            self.password,
            prompt_mfa=prompt,
            retry_attempts=4,
            retry_min_wait=2,
            retry_max_wait=30,
        )
        self._client.login(str(self.token_store))
        if token_file.exists():
            os.chmod(token_file, 0o600)
        self.password = None
        logger.info("Garmin authentication succeeded", extra={"token_cache": str(token_file)})

    @property
    def client(self) -> Any:
        if self._client is None:
            raise RuntimeError("Garmin provider is not authenticated")
        return self._client

    def _call(self, method: str, *args: Any, **kwargs: Any) -> Any:
        if self.request_delay_seconds:
            time.sleep(self.request_delay_seconds)
        return getattr(self.client, method)(*args, **kwargs)

    def get_daily_summary(self, target: date) -> dict[str, Any]:
        return self._call("get_stats", target.isoformat())

    def get_sleep(self, target: date) -> dict[str, Any]:
        return self._call("get_sleep_data", target.isoformat())

    def get_hrv(self, target: date) -> dict[str, Any] | None:
        return self._call("get_hrv_data", target.isoformat())

    def get_body_battery(self, target: date) -> Any:
        value = target.isoformat()
        return self._call("get_body_battery", value, value)

    def get_stress(self, target: date) -> dict[str, Any]:
        return self._call("get_stress_data", target.isoformat())

    def get_training_readiness(self, target: date) -> Any:
        return self._call("get_morning_training_readiness", target.isoformat())

    def get_training_status(self, target: date) -> dict[str, Any]:
        return self._call("get_training_status", target.isoformat())

    def get_vo2max(self, target: date) -> dict[str, Any]:
        return self._call("get_max_metrics", target.isoformat())

    def get_fitness_age(self, target: date) -> dict[str, Any]:
        return self._call("get_fitnessage_data", target.isoformat())

    def get_lactate_threshold(self, target: date) -> Any:
        return self._call(
            "get_lactate_threshold",
            latest=False,
            start_date=target,
            end_date=target,
            aggregation="daily",
        )

    def get_activities(self, start_date: date, end_date: date) -> list[dict[str, Any]]:
        return self._call(
            "get_activities_by_date",
            start_date.isoformat(),
            end_date.isoformat(),
        )

    def get_activity(self, activity_id: int) -> dict[str, Any]:
        return self._call("get_activity", str(activity_id))

    def download_fit(self, activity_id: int) -> bytes:
        from garminconnect import Garmin

        return self._call(
            "download_activity",
            str(activity_id),
            Garmin.ActivityDownloadFormat.ORIGINAL,
        )


def extract_fit_bytes(download: bytes) -> bytes:
    """Return the FIT member from Garmin's original ZIP download."""
    if not download:
        raise ValueError("Garmin returned an empty activity download")
    if not zipfile.is_zipfile(io.BytesIO(download)):
        return download
    with zipfile.ZipFile(io.BytesIO(download)) as archive:
        members = [item for item in archive.infolist() if item.filename.lower().endswith(".fit")]
        if not members:
            raise ValueError("Garmin activity archive contained no FIT file")
        members.sort(key=lambda item: item.file_size, reverse=True)
        if members[0].file_size > 100 * 1024 * 1024:
            raise ValueError("FIT member exceeds the 100 MiB safety limit")
        return archive.read(members[0])


def archive_fit(download: bytes, root: Path, activity_id: int, activity_date: date) -> Path:
    destination = (
        root / f"{activity_date.year:04d}" / f"{activity_date.month:02d}" / f"{activity_id}.fit"
    )
    if destination.is_file() and destination.stat().st_size > 0:
        return destination
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
    payload = extract_fit_bytes(download)
    temporary = destination.with_suffix(".fit.part")
    with temporary.open("wb") as handle:
        os.chmod(temporary, 0o600)
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(destination)
    return destination
