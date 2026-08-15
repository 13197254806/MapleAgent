from __future__ import annotations

import asyncio
import json
import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import pymysql
from pymysql.connections import Connection

from .clock import epoch_ms
from .config import DatabaseConfig, ServerAppConfig
from .decision import DecisionRecord
from .models import ActionPlan, PlanAck

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class DatabaseOperation:
    kind: Literal[
        "open_session",
        "client_connected",
        "close_session",
        "event",
        "plan",
        "ack",
    ]
    values: tuple[Any, ...]


class MySQLPersistence:
    """Single-threaded, queued MySQL audit writer kept off the CV event loop."""

    def __init__(self, config: DatabaseConfig):
        self.config = config
        self._queue: asyncio.Queue[DatabaseOperation | None] = asyncio.Queue(
            maxsize=config.queue_capacity
        )
        self._executor: ThreadPoolExecutor | None = None
        self._worker_task: asyncio.Task[None] | None = None
        self._connection: Connection | None = None
        self._started = False
        self._last_error: str | None = None

    @property
    def health(self) -> dict[str, object]:
        return {
            "enabled": self.config.enabled,
            "connected": self._started and self._connection is not None,
            "queued_writes": self._queue.qsize(),
            "last_error": self._last_error,
        }

    async def start(self) -> None:
        if not self.config.enabled:
            return
        self._executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="maple-agent-mysql"
        )
        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(self._executor, self._connect)
        except Exception:
            self._executor.shutdown(wait=True)
            self._executor = None
            raise
        self._started = True
        self._worker_task = asyncio.create_task(
            self._worker(), name="mysql-persistence-writer"
        )

    async def close(self) -> None:
        if not self._started:
            return
        await self._queue.put(None)
        if self._worker_task is not None:
            await self._worker_task
        loop = asyncio.get_running_loop()
        if self._executor is not None:
            await loop.run_in_executor(self._executor, self._disconnect)
            self._executor.shutdown(wait=True)
        self._executor = None
        self._worker_task = None
        self._started = False

    def open_session(
        self,
        session_id: str,
        config: ServerAppConfig,
        recording_path: Path,
    ) -> None:
        self._enqueue(
            DatabaseOperation(
                kind="open_session",
                values=(
                    session_id,
                    epoch_ms(),
                    str(recording_path),
                    json.dumps(config.model_dump(mode="json"), ensure_ascii=False),
                ),
            )
        )

    def client_connected(self, session_id: str, client_version: str) -> None:
        self._enqueue(
            DatabaseOperation(
                kind="client_connected", values=(client_version, session_id)
            )
        )

    def close_session(self, session_id: str, status: str, last_frame_id: int) -> None:
        self._enqueue(
            DatabaseOperation(
                kind="close_session",
                values=(epoch_ms(), status, last_frame_id, session_id),
            )
        )

    def record_event(self, session_id: str, event_type: str, **fields: Any) -> None:
        self._enqueue(
            DatabaseOperation(
                kind="event",
                values=(
                    session_id,
                    epoch_ms(),
                    event_type,
                    json.dumps(fields, ensure_ascii=False, default=str),
                ),
            )
        )

    def record_plan(self, plan: ActionPlan, decision: DecisionRecord) -> None:
        self._enqueue(
            DatabaseOperation(
                kind="plan",
                values=(
                    plan.session_id,
                    plan.plan_id,
                    plan.based_on_frame_id,
                    plan.created_at_ms,
                    plan.ttl_ms,
                    decision.state.value,
                    decision.intent.type.value,
                    plan.expected_result,
                    plan.model_dump_json(),
                ),
            )
        )

    def record_ack(self, ack: PlanAck) -> None:
        self._enqueue(
            DatabaseOperation(
                kind="ack",
                values=(
                    ack.status.value,
                    ack.detail,
                    ack.sent_at_ms,
                    ack.session_id,
                    ack.plan_id,
                ),
            )
        )

    def _enqueue(self, operation: DatabaseOperation) -> None:
        if not self.config.enabled:
            return
        if not self._started:
            raise RuntimeError("MySQL persistence has not been started")
        try:
            self._queue.put_nowait(operation)
        except asyncio.QueueFull:
            LOGGER.error("MySQL persistence queue is full; dropping %s", operation.kind)

    async def _worker(self) -> None:
        assert self._executor is not None
        loop = asyncio.get_running_loop()
        while True:
            operation = await self._queue.get()
            if operation is None:
                self._queue.task_done()
                return
            try:
                await loop.run_in_executor(
                    self._executor, self._apply_with_retry, operation
                )
                self._last_error = None
            except Exception as exc:
                self._last_error = f"{type(exc).__name__}: {exc}"
                LOGGER.exception("MySQL persistence write failed: %s", operation.kind)
            finally:
                self._queue.task_done()

    def _connect(self) -> None:
        self._connection = pymysql.connect(
            host=self.config.host,
            port=self.config.port,
            user=self.config.user,
            password=self.config.resolved_password(),
            database=self.config.database,
            charset="utf8mb4",
            autocommit=True,
            connect_timeout=self.config.connect_timeout_seconds,
        )
        with self._connection.cursor() as cursor:
            cursor.execute("SELECT 1 FROM sessions LIMIT 0")
            cursor.execute("SELECT 1 FROM session_events LIMIT 0")
            cursor.execute("SELECT 1 FROM action_plans LIMIT 0")

    def _disconnect(self) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None

    def _apply_with_retry(self, operation: DatabaseOperation) -> None:
        try:
            self._apply(operation)
        except pymysql.MySQLError:
            self._disconnect()
            self._connect()
            self._apply(operation)

    def _apply(self, operation: DatabaseOperation) -> None:
        if self._connection is None:
            raise RuntimeError("MySQL connection is not available")
        self._connection.ping(reconnect=True)
        sql = {
            "open_session": """
                INSERT INTO sessions
                    (session_id, started_at_ms, status, recording_path, config_json)
                VALUES (%s, %s, 'starting', %s, CAST(%s AS JSON))
                ON DUPLICATE KEY UPDATE
                    started_at_ms = %s,
                    ended_at_ms = NULL,
                    status = 'starting',
                    recording_path = %s,
                    config_json = CAST(%s AS JSON)
            """,
            "client_connected": """
                UPDATE sessions
                SET client_version = %s, status = 'running'
                WHERE session_id = %s
            """,
            "close_session": """
                UPDATE sessions
                SET ended_at_ms = %s, status = %s, last_frame_id = %s
                WHERE session_id = %s
            """,
            "event": """
                INSERT INTO session_events
                    (session_id, occurred_at_ms, event_type, details_json)
                VALUES (%s, %s, %s, CAST(%s AS JSON))
            """,
            "plan": """
                INSERT INTO action_plans
                    (session_id, plan_id, based_on_frame_id, created_at_ms, ttl_ms,
                     fsm_state, intent_type, expected_result, plan_json)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, CAST(%s AS JSON))
            """,
            "ack": """
                UPDATE action_plans
                SET ack_status = %s, ack_detail = %s, acked_at_ms = %s
                WHERE session_id = %s AND plan_id = %s
            """,
        }[operation.kind]
        values = operation.values
        if operation.kind == "open_session":
            session_id, started_at_ms, recording_path, config_json = values
            values = (
                session_id,
                started_at_ms,
                recording_path,
                config_json,
                started_at_ms,
                recording_path,
                config_json,
            )
        with self._connection.cursor() as cursor:
            cursor.execute(sql, values)
