"""Independent optional enrichment: exceptions cannot redefine core sync success."""

import logging
from pathlib import Path

from .enrichment import ObservationStore
from .fit_store import FitStore

logger = logging.getLogger(__name__)


class CollectorAnalysis:
    def __init__(self, database, provider):
        self.database, self.provider = database, provider
        self.store = ObservationStore(database)
        self.optional_calls_suspended = False

    def _run(self, resource, activity_id, call):
        try:
            return call()
        except Exception as error:
            # Database outages are reported safely too; do not expose token/error payload text.
            logger.warning(
                "Optional analysis failed",
                extra={
                    "resource": resource,
                    "activity_id": activity_id,
                    "error_type": type(error).__name__,
                },
            )
            return {"status": "error", "error_code": type(error).__name__}

    def profile(self):
        self.optional_calls_suspended = False
        result = self._run(
            "training_profile", None, lambda: self.store.collect("training_profile", self.provider)
        )
        self._check_limits("training_profile", None)
        return result

    def _check_limits(self, resource, activity_id):
        state = self.store.state(resource, activity_id)
        code = state["error_code"] if state else None
        if code and ("Authentication" in code or "TooManyRequests" in code):
            self.optional_calls_suspended = True

    def activity(self, activity_id, path):
        if path:
            result = self._run(
                "fit", activity_id, lambda: FitStore(self.database).process(activity_id, Path(path))
            )
            if result and result.get("status") == "success":
                self.store.attempt(
                    "weather_historical", activity_id, "pending", "fit_generation_changed"
                )
        if self.optional_calls_suspended:
            self.store.attempt(
                "weather_recorded",
                activity_id,
                "error",
                "optional_calls_suspended_auth_or_rate_limit",
            )
            return "error"
        result = self._run(
            "weather_recorded",
            activity_id,
            lambda: self.store.collect("weather_recorded", self.provider, activity_id),
        )
        self._check_limits("weather_recorded", activity_id)
        return result
