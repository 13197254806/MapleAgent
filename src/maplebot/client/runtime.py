from __future__ import annotations

import asyncio
import json
import logging
import uuid
from contextlib import suppress

from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed

from .. import __version__
from ..clock import epoch_ms, monotonic_ms
from ..config import ClientAppConfig
from ..discovery import discover_server
from ..models import (
    ActionPlan,
    ErrorMessage,
    FrameHeader,
    HeartbeatAck,
    HeartbeatMessage,
    HelloMessage,
    PlanAck,
    StopMessage,
    encode_frame,
)
from .capture import WindowCapture
from .input import ActionExecutor, Win32WindowInput, emergency_key_pressed
from .window import TargetWindow

LOGGER = logging.getLogger(__name__)


class WireSender:
    def __init__(self, websocket: ClientConnection, session_id: str):
        self.websocket = websocket
        self.session_id = session_id
        self._seq = 0
        self._lock = asyncio.Lock()

    async def send_model(self, model_type: type, **fields: object) -> None:
        async with self._lock:
            model = model_type(session_id=self.session_id, seq=self._seq, **fields)
            self._seq += 1
            await self.websocket.send(model.model_dump_json())

    async def send_frame(self, frame_id: int, captured: object) -> None:
        async with self._lock:
            header = FrameHeader(
                session_id=self.session_id,
                seq=self._seq,
                frame_id=frame_id,
                captured_at_ms=captured.captured_at_ms,
                width=captured.width,
                height=captured.height,
                window_rect=captured.window_rect,
            )
            self._seq += 1
            await self.websocket.send(encode_frame(header, captured.jpeg))


class ClientRuntime:
    def __init__(self, config: ClientAppConfig):
        self.config = config
        self.session_id = str(uuid.uuid4())
        self.target = TargetWindow(
            config.client.process_name, config.client.window_title
        )
        self.input = Win32WindowInput(self.target)
        self.capture = WindowCapture(
            self.target,
            config.frame.width,
            config.frame.height,
            config.client.jpeg_quality,
            config.client.capture_backend,
        )
        self.executor = ActionExecutor(self.session_id, config.input, self.input)
        self.shutdown = asyncio.Event()
        self.last_frame_id = -1
        self.last_server_message_ms = monotonic_ms()

    async def run(self) -> None:
        emergency_task = asyncio.create_task(
            self._emergency_monitor(), name="emergency-key"
        )
        try:
            while not self.shutdown.is_set():
                try:
                    await self._run_connection()
                except (OSError, ConnectionClosed, RuntimeError, TimeoutError) as exc:
                    LOGGER.warning("connection ended: %s", exc)
                finally:
                    self.executor.emergency_stop()
                if not self.shutdown.is_set():
                    await asyncio.sleep(self.config.client.reconnect_delay_ms / 1000)
        finally:
            self.shutdown.set()
            emergency_task.cancel()
            await asyncio.gather(emergency_task, return_exceptions=True)
            self.executor.emergency_stop()

    async def _run_connection(self) -> None:
        self.executor.emergency_stop()
        self.session_id = str(uuid.uuid4())
        self.executor = ActionExecutor(self.session_id, self.config.input, self.input)
        self.last_frame_id = -1
        server_url = await self._resolve_server_url()
        url = f"{server_url.rstrip('/')}/{self.session_id}"
        LOGGER.info(
            "target process=%s hwnd=0x%X capture=%s",
            self.config.client.process_name,
            self.target.hwnd,
            self.config.client.capture_backend,
        )
        async with connect(url, max_size=self.config.frame.max_bytes) as websocket:
            sender = WireSender(websocket, self.session_id)
            await sender.send_model(
                HelloMessage,
                client_version=__version__,
                target_width=self.config.frame.width,
                target_height=self.config.frame.height,
            )
            self.last_server_message_ms = monotonic_ms()
            connection_stop = asyncio.Event()
            plans: asyncio.Queue[ActionPlan] = asyncio.Queue(maxsize=1)
            tasks = [
                asyncio.create_task(
                    self._frame_loop(sender, connection_stop), name="frames"
                ),
                asyncio.create_task(
                    self._heartbeat_loop(sender, connection_stop), name="heartbeat"
                ),
                asyncio.create_task(
                    self._receive_loop(websocket, plans, connection_stop),
                    name="receiver",
                ),
                asyncio.create_task(
                    self._execute_loop(sender, plans, connection_stop), name="executor"
                ),
                asyncio.create_task(
                    self._watchdog_loop(connection_stop), name="client-watchdog"
                ),
            ]
            _done, pending = await asyncio.wait(
                tasks, return_when=asyncio.FIRST_COMPLETED
            )
            connection_stop.set()
            self.executor.emergency_stop()
            for task in pending:
                task.cancel()
            results = await asyncio.gather(*tasks, return_exceptions=True)
            if self.shutdown.is_set():
                with suppress(Exception):
                    await sender.send_model(StopMessage, reason="local emergency stop")
            for result in results:
                if isinstance(result, Exception) and not isinstance(
                    result, asyncio.CancelledError
                ):
                    with suppress(Exception):
                        await sender.send_model(
                            ErrorMessage,
                            code="CLIENT_TASK_ERROR",
                            detail=f"{type(result).__name__}: {result}"[:500],
                        )
                    raise result

    async def _resolve_server_url(self) -> str:
        if self.config.client.server_url:
            return self.config.client.server_url
        LOGGER.info(
            "searching for '%s' on UDP %s",
            self.config.discovery.service_name,
            self.config.discovery.port,
        )
        server = await asyncio.to_thread(discover_server, self.config.discovery)
        LOGGER.info(
            "discovered server %s at %s",
            server.instance_id,
            server.websocket_url,
        )
        return server.websocket_url

    async def _frame_loop(self, sender: WireSender, stop: asyncio.Event) -> None:
        interval = 1 / self.config.client.fps
        while not stop.is_set() and not self.shutdown.is_set():
            started = asyncio.get_running_loop().time()
            captured = await asyncio.to_thread(self.capture.capture)
            self.last_frame_id += 1
            await sender.send_frame(self.last_frame_id, captured)
            remaining = interval - (asyncio.get_running_loop().time() - started)
            if remaining > 0:
                await asyncio.sleep(remaining)

    async def _heartbeat_loop(self, sender: WireSender, stop: asyncio.Event) -> None:
        while not stop.is_set() and not self.shutdown.is_set():
            await sender.send_model(
                HeartbeatMessage,
                last_frame_id=self.last_frame_id,
                last_plan_id=self.executor.last_plan_id,
            )
            await asyncio.sleep(self.config.client.heartbeat_interval_ms / 1000)

    async def _receive_loop(
        self,
        websocket: ClientConnection,
        plans: asyncio.Queue[ActionPlan],
        stop: asyncio.Event,
    ) -> None:
        last_server_seq = -1
        async for raw in websocket:
            self.last_server_message_ms = monotonic_ms()
            if not isinstance(raw, str):
                continue
            payload = json.loads(raw)
            if payload.get("session_id") != self.session_id:
                raise ValueError("server session_id mismatch")
            seq = payload.get("seq")
            if not isinstance(seq, int) or seq <= last_server_seq:
                raise ValueError("non-increasing server sequence")
            last_server_seq = seq
            message_type = payload.get("type")
            if message_type == "action_plan":
                plan = ActionPlan.model_validate(payload)
                if plans.full():
                    await plans.get()
                    self.executor.emergency_stop()
                await plans.put(plan)
            elif message_type == "stop":
                StopMessage.model_validate(payload)
                self.executor.emergency_stop()
                stop.set()
                return
            elif message_type == "heartbeat_ack":
                heartbeat = HeartbeatAck.model_validate(payload)
                client_received_at_ms = epoch_ms()
                estimate = (
                    (heartbeat.server_received_at_ms - heartbeat.echo_client_sent_at_ms)
                    + (heartbeat.sent_at_ms - client_received_at_ms)
                ) / 2
                if self.executor.server_clock_offset_ms is None:
                    self.executor.server_clock_offset_ms = estimate
                else:
                    self.executor.server_clock_offset_ms = (
                        self.executor.server_clock_offset_ms * 0.8 + estimate * 0.2
                    )
            elif message_type == "error":
                error = ErrorMessage.model_validate(payload)
                raise RuntimeError(f"server error {error.code}: {error.detail}")

    async def _execute_loop(
        self, sender: WireSender, plans: asyncio.Queue[ActionPlan], stop: asyncio.Event
    ) -> None:
        while not stop.is_set() and not self.shutdown.is_set():
            plan = await plans.get()
            status, detail = await self.executor.execute(plan)
            await sender.send_model(
                PlanAck, plan_id=plan.plan_id, status=status, detail=detail
            )

    async def _watchdog_loop(self, stop: asyncio.Event) -> None:
        while not stop.is_set() and not self.shutdown.is_set():
            await asyncio.sleep(0.1)
            elapsed = monotonic_ms() - self.last_server_message_ms
            if elapsed > self.config.client.watchdog_timeout_ms:
                self.executor.emergency_stop()
                stop.set()
                raise TimeoutError(f"server watchdog expired after {elapsed} ms")

    async def _emergency_monitor(self) -> None:
        while not self.shutdown.is_set():
            if emergency_key_pressed(self.config.client.emergency_key):
                LOGGER.error("local emergency stop triggered")
                self.executor.emergency_stop()
                self.shutdown.set()
                return
            await asyncio.sleep(0.05)
