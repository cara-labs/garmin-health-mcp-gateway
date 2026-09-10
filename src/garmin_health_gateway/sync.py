from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterator
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .config import Settings
from .db import Database
from .normalize import normalize_activity, normalize_daily
from .provider import GarminProvider, archive_fit
from .sync_requests import SyncRequests

logger = logging.getLogger(__name__)


def date_range(start: date, end: date) -> Iterator[date]:
    current = start
    while current <= end:
        yield current
        current += timedelta(days=1)


class SyncEngine:
    def __init__(self, settings: Settings, database: Database, provider: GarminProvider):
        self.settings = settings
        self.database = database
        self.provider = provider

    def _optional(self, resource: str, target: date, call: Callable[[], Any]) -> Any:
        try:
            return call()
        except Exception as error:
            error_name = type(error).__name__
            if "Authentication" in error_name or "TooManyRequests" in error_name:
                raise
            logger.warning(
                "Optional Garmin resource unavailable",
                extra={
                    "resource": resource,
                    "date": target.isoformat(),
                    "error_type": error_name,
                    "error": str(error),
                },
            )
            return None

    def sync_daily(self, start: date, end: date) -> int:
        resource = "daily_health"
        self.database.start_sync(resource)
        count = 0
        try:
            for target in date_range(start, end):
                payload = {
                    "summary": self.provider.get_daily_summary(target),
                    "sleep": self._optional(
                        "sleep", target, lambda target=target: self.provider.get_sleep(target)
                    ),
                    "hrv": self._optional(
                        "hrv", target, lambda target=target: self.provider.get_hrv(target)
                    ),
                    "body_battery": self._optional(
                        "body_battery",
                        target,
                        lambda target=target: self.provider.get_body_battery(target),
                    ),
                    "stress": self._optional(
                        "stress", target, lambda target=target: self.provider.get_stress(target)
                    ),
                    "training_readiness": self._optional(
                        "training_readiness",
                        target,
                        lambda target=target: self.provider.get_training_readiness(target),
                    ),
                    "training_status": self._optional(
                        "training_status",
                        target,
                        lambda target=target: self.provider.get_training_status(target),
                    ),
                    "vo2max": self._optional(
                        "vo2max", target, lambda target=target: self.provider.get_vo2max(target)
                    ),
                    "fitness_age": self._optional(
                        "fitness_age",
                        target,
                        lambda target=target: self.provider.get_fitness_age(target),
                    ),
                    "lactate_threshold": (
                        self._optional(
                            "lactate_threshold",
                            target,
                            lambda target=target: self.provider.get_lactate_threshold(target),
                        )
                        if target == end
                        else None
                    ),
                }
                health, activity = normalize_daily(target, payload)
                self.database.upsert_daily_health(health)
                self.database.upsert_daily_activity(activity)
                count += 1
                logger.info(
                    "Daily Garmin data synchronized",
                    extra={"date": target.isoformat(), "completed": count},
                )
            self.database.finish_sync(resource)
            return count
        except Exception as error:
            self.database.finish_sync(resource, f"{type(error).__name__}: {error}")
            raise

    def _existing_fit_path(self, activity_id: int, activity_date: date) -> Path:
        return (
            self.settings.fit_archive
            / f"{activity_date.year:04d}"
            / f"{activity_date.month:02d}"
            / f"{activity_id}.fit"
        )

    def sync_activities(self, start: date, end: date) -> int:
        resource = "activities"
        self.database.start_sync(resource)
        count = 0
        try:
            activities = self.provider.get_activities(start, end)
            for source in activities:
                normalized = normalize_activity(source, timezone=self.settings.timezone)
                activity_id = normalized["garmin_activity_id"]
                activity_date = normalized["start_time"].date()
                fit_path = self._existing_fit_path(activity_id, activity_date)
                if not fit_path.is_file() or fit_path.stat().st_size == 0:
                    try:
                        download = self.provider.download_fit(activity_id)
                        fit_path = archive_fit(
                            download,
                            self.settings.fit_archive,
                            activity_id,
                            activity_date,
                        )
                    except Exception as error:
                        error_name = type(error).__name__
                        if "Authentication" in error_name or "TooManyRequests" in error_name:
                            raise
                        normalized["fit_download_status"] = "error"
                        normalized["fit_download_error"] = f"{error_name}: {error}"[:2000]
                        logger.warning(
                            "Original FIT file was unavailable",
                            extra={
                                "activity_id": activity_id,
                                "error_type": error_name,
                                "error": str(error),
                            },
                        )
                    else:
                        normalized["fit_file_path"] = str(fit_path)
                        normalized["fit_download_status"] = "archived"
                else:
                    normalized["fit_file_path"] = str(fit_path)
                    normalized["fit_download_status"] = "archived"
                self.database.upsert_activity(normalized)
                count += 1
                logger.info(
                    "Garmin activity synchronized",
                    extra={"activity_id": activity_id, "completed": count},
                )
            self.database.finish_sync(resource)
            return count
        except Exception as error:
            self.database.finish_sync(resource, f"{type(error).__name__}: {error}")
            raise

    def run_once(self, *, force_backfill: bool = False) -> dict[str, int]:
        with SyncRequests(self.settings.sync_request_dir).lock("worker", blocking=False):
            return self._run_once(force_backfill=force_backfill)

    def _run_once(self, *, force_backfill: bool = False) -> dict[str, int]:
        today = datetime.now(ZoneInfo(self.settings.timezone)).date()
        daily_initial = force_backfill or not self.database.has_successful_sync("daily_health")
        activity_initial = force_backfill or not self.database.has_successful_sync("activities")
        daily_days = (
            self.settings.daily_backfill_days if daily_initial else self.settings.recent_days
        )
        activity_days = (
            self.settings.activity_backfill_days if activity_initial else self.settings.recent_days
        )
        daily_start = today - timedelta(days=daily_days - 1)
        activity_start = today - timedelta(days=activity_days - 1)
        return {
            "daily_health": self.sync_daily(daily_start, today),
            "activities": self.sync_activities(activity_start, today),
        }

    def process_request(self, requests: SyncRequests) -> bool:
        with requests.lock("worker", blocking=False):
            if not requests.claim():
                return False
            today = datetime.now(ZoneInfo(self.settings.timezone)).date()
            start = today - timedelta(days=2)
            counts = {}
            errors = {}
            # Try both resources even if one fails; stop external calls on auth/rate limits.
            for name, sync in (
                ("daily_health", self.sync_daily),
                ("activities", self.sync_activities),
            ):
                try:
                    counts[name] = sync(start, today)
                except Exception as error:
                    kind = type(error).__name__
                    errors[name] = kind
                    logger.exception("Ad hoc synchronization failed", extra={"resource": name})
                    if "Authentication" in kind or "TooManyRequests" in kind:
                        break
            requests.finish(
                {
                    "status": "error" if errors else "success",
                    "counts": counts,
                    "errors": errors,
                    "start_date": start.isoformat(),
                    "end_date": today.isoformat(),
                }
            )
            return True

    def run_forever(self) -> None:
        requests = SyncRequests(self.settings.sync_request_dir)
        # Also excludes a second scheduler while the first sleeps between cycles.
        with requests.lock("scheduler", blocking=False):
            with requests.lock("worker", blocking=False):
                requests.recover()
            self._schedule(requests)

    def _schedule(self, requests: SyncRequests) -> None:
        next_scheduled = 0.0
        backfill_pending = not all(
            self.database.has_successful_sync(resource)
            for resource in ("daily_health", "activities")
        )
        while True:
            try:
                if not backfill_pending and self.process_request(requests):
                    # Keep the scheduled full backfill independent of a recent-only request.
                    continue
                if time.monotonic() >= next_scheduled:
                    started = time.monotonic()
                    try:
                        result = self.run_once(force_backfill=backfill_pending)
                        backfill_pending = False
                        logger.info("Synchronization cycle completed", extra=result)
                    finally:
                        next_scheduled = max(
                            time.monotonic() + 30,
                            started + self.settings.sync_interval_seconds,
                        )
            except Exception:
                logger.exception("Synchronization cycle failed; it will be retried")
            time.sleep(2)
