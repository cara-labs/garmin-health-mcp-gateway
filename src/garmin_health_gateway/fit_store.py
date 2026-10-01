"""Bounded, restart-safe FIT staging with an atomic active-generation pointer."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from pathlib import Path
from typing import Any

from psycopg import Connection
from psycopg.types.json import Jsonb

from .db import Database
from .fit_decoder import (
    BATCH_SIZE,
    DECODER_VERSION,
    SCHEMA_VERSION,
    DecodedMessage,
    FitDecoder,
    FitLimitError,
    archive_identity,
)


def _batches(rows: Iterable[DecodedMessage]) -> Iterable[list[DecodedMessage]]:
    batch = []
    for row in rows:
        batch.append(row)
        if len(batch) == BATCH_SIZE:
            yield batch
            batch = []
    if batch:
        yield batch


class FitStore:
    def __init__(self, database: Database, decoder: FitDecoder | None = None):
        self.database = database
        self.decoder = decoder or FitDecoder()

    @staticmethod
    def _stage(conn: Connection, generation: str, batch: list[DecodedMessage]) -> None:
        with conn.cursor() as cursor:
            cursor.executemany(
                "INSERT INTO fit_messages VALUES (%s,%s,%s,%s,%s,%s)",
                [
                    (generation, r.ordinal, r.number, r.name, r.observed_at, Jsonb(r.fields))
                    for r in batch
                ],
            )
            cursor.executemany(
                "INSERT INTO fit_samples VALUES (%s,%s,%s,%s,%s,%s)",
                [
                    (
                        generation,
                        r.ordinal,
                        r.observed_at,
                        r.elapsed_seconds,
                        r.timer_seconds,
                        Jsonb(r.data),
                    )
                    for r in batch
                    if r.category == "sample"
                ],
            )
            cursor.executemany(
                "INSERT INTO fit_timer_events VALUES (%s,%s,%s,%s,%s,%s)",
                [
                    (
                        generation,
                        r.ordinal,
                        r.observed_at,
                        r.elapsed_seconds,
                        r.data.get("event_type"),
                        Jsonb(r.data),
                    )
                    for r in batch
                    if r.category == "timer"
                ],
            )
            cursor.executemany(
                "INSERT INTO fit_laps VALUES (%s,%s,%s)",
                [(generation, r.ordinal, Jsonb(r.data)) for r in batch if r.category == "lap"],
            )
            cursor.executemany(
                "INSERT INTO fit_workout_steps VALUES (%s,%s,%s,%s)",
                [
                    (
                        generation,
                        r.ordinal,
                        "planned" if r.category == "planned_step" else "executed",
                        Jsonb(r.data),
                    )
                    for r in batch
                    if r.category in ("planned_step", "executed_step")
                    or (r.category == "lap" and r.data["native"].get("wkt_step_index") is not None)
                ],
            )
            cursor.executemany(
                "INSERT INTO fit_extra_metrics VALUES (%s,%s,%s,%s)",
                [
                    (generation, r.ordinal, r.name or str(r.number), Jsonb(r.data))
                    for r in batch
                    if r.category == "metric"
                    or (
                        r.category == "sample"
                        and any(f["developer_data_index"] is not None for f in r.fields)
                    )
                ],
            )
        conn.commit()  # bounded transactions; staging is never used by query tools

    def process(self, activity_id: int, path: Path, *, reprocess: bool = False) -> dict[str, Any]:
        generation = None
        # Session lock remains on this specific connection across staging commits.
        lock_key = activity_id % (2**31 - 1)
        with self.database.connection() as conn:
            conn.execute("SELECT pg_advisory_lock(719042882,%s)", (lock_key,))
            conn.commit()
            try:
                row = conn.execute(
                    "SELECT start_time FROM activities WHERE garmin_activity_id=%s", (activity_id,)
                ).fetchone()
                if row is None:
                    return {"status": "not_found", "activity_id": activity_id}
                start: datetime = row["start_time"]
                conn.execute(
                    "INSERT INTO fit_processing_state(activity_id,status) VALUES (%s,'running') "
                    "ON CONFLICT(activity_id) DO UPDATE SET status='running', "
                    "last_attempt_at=now(),error_code=NULL",
                    (activity_id,),
                )
                # A crashed staging generation is unpublished and cannot be resumed
                # blindly. Restart safely from the archive; retain it as error evidence.
                conn.execute(
                    "UPDATE fit_generations SET status='error',error_code='interrupted', "
                    "completed_at=now() WHERE activity_id=%s AND status='staging'",
                    (activity_id,),
                )
                conn.commit()
                digest, size = archive_identity(path)
                active = conn.execute(
                    "SELECT g.* FROM fit_active_generations a JOIN fit_generations g "
                    "ON g.id=a.generation_id WHERE a.activity_id=%s",
                    (activity_id,),
                ).fetchone()
                if (
                    not reprocess
                    and active
                    and active["archive_sha256"] == digest
                    and active["decoder_version"] == DECODER_VERSION
                    and active["schema_version"] == SCHEMA_VERSION
                ):
                    self._success(conn, activity_id)
                    return {
                        "status": "unchanged",
                        "activity_id": activity_id,
                        "generation_id": str(active["id"]),
                    }
                result = conn.execute(
                    "INSERT INTO fit_generations(activity_id,archive_sha256,decoder_version, "
                    "schema_version,status,archive_bytes) VALUES (%s,%s,%s,%s,'staging',%s) "
                    "RETURNING id",
                    (activity_id, digest, DECODER_VERSION, SCHEMA_VERSION, size),
                ).fetchone()
                generation = result["id"]
                conn.commit()
                count = 0
                for batch in _batches(self.decoder.decode(path, start)):
                    self._stage(conn, generation, batch)
                    count += len(batch)
                if archive_identity(path) != (digest, size):
                    raise ValueError("archive_changed_during_processing")
                conn.execute(
                    "UPDATE fit_generations SET status='ready',completed_at=now(),message_count=%s "
                    "WHERE id=%s",
                    (count, generation),
                )
                conn.execute(
                    "INSERT INTO fit_active_generations(activity_id,generation_id) VALUES (%s,%s) "
                    "ON CONFLICT(activity_id) DO UPDATE SET generation_id=excluded.generation_id, "
                    "published_at=now()",
                    (activity_id, generation),
                )
                # Pointer, completed generation, and success status commit together.
                self._success(conn, activity_id)
                return {
                    "status": "success",
                    "activity_id": activity_id,
                    "generation_id": str(generation),
                    "message_count": count,
                }
            except BaseException as error:
                conn.rollback()
                code = str(error) if isinstance(error, FitLimitError) else type(error).__name__
                if generation is not None:
                    conn.execute(
                        "UPDATE fit_generations SET status='error',completed_at=now(),"
                        "error_code=%s "
                        "WHERE id=%s",
                        (code, generation),
                    )
                conn.execute(
                    "UPDATE fit_processing_state SET status='error',error_code=%s "
                    "WHERE activity_id=%s",
                    (code, activity_id),
                )
                conn.commit()
                if not isinstance(error, Exception):
                    raise
                return {
                    "status": "error",
                    "activity_id": activity_id,
                    "generation_id": str(generation) if generation else None,
                    "error_code": code,
                }
            finally:
                conn.rollback()
                conn.execute("SELECT pg_advisory_unlock(719042882,%s)", (lock_key,))
                conn.commit()

    @staticmethod
    def _success(conn: Connection, activity_id: int) -> None:
        conn.execute(
            "UPDATE fit_processing_state SET status='success',last_success_at=now(), "
            "error_code=NULL WHERE activity_id=%s",
            (activity_id,),
        )
        conn.commit()
