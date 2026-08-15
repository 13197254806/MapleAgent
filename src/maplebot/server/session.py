from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from fastapi import WebSocket, WebSocketDisconnect

from ..clock import epoch_ms, monotonic_ms
from ..config import ServerAppConfig
from ..decision import ActionPlanner, DecisionEngine
from ..map_service import MappingTrace, MapService
from ..models import (
    AckStatus,
    ErrorMessage,
    HeartbeatAck,
    HeartbeatMessage,
    HelloMessage,
    PlanAck,
    StopMessage,
    decode_frame,
    parse_client_message,
)
from ..perception import Perception, decode_jpeg
from ..persistence import MySQLPersistence
from ..recorder import Recorder
from ..world import WorldStateBuilder


@dataclass
class SessionContext:
    session_id: str
    websocket: WebSocket
    config: ServerAppConfig
    map_service: MapService
    perception: Perception
    recorder: Recorder
    database: MySQLPersistence
    world_builder: WorldStateBuilder = field(init=False)
    decision: DecisionEngine = field(init=False)
    planner: ActionPlanner = field(init=False)
    mapping_trace: MappingTrace = field(init=False)
    last_seen_monotonic_ms: int = field(default_factory=monotonic_ms)
    last_client_seq: int = -1
    server_seq: int = 0
    last_frame_id: int = -1
    pending_plan_id: int | None = None
    stopping: bool = False
    client_clock_offset_ms: int = 0
    close_status: str = "closed"

    def __post_init__(self) -> None:
        self.world_builder = WorldStateBuilder(self.config.perception, self.map_service)
        self.decision = DecisionEngine(
            self.config.control,
            self.map_service,
            self.config.perception.player_missing_limit,
        )
        self.planner = ActionPlanner(self.config.control)
        model = self.map_service.model
        self.mapping_trace = MappingTrace(
            model.name,
            model.minimap_width,
            model.minimap_height,
            self.config.map.mapping_node_distance,
        )

    async def run(self) -> None:
        watchdog = asyncio.create_task(
            self._watchdog(), name=f"watchdog-{self.session_id}"
        )
        try:
            hello = await self._receive_hello()
            self.database.client_connected(self.session_id, hello.client_version)
            self._event("client_connected", client_version=hello.client_version)
            await self._send_heartbeat_ack(hello.sent_at_ms)
            while not self.stopping:
                message = await self.websocket.receive()
                self.last_seen_monotonic_ms = monotonic_ms()
                if message.get("type") == "websocket.disconnect":
                    self.close_status = "disconnected"
                    self._event("client_disconnected")
                    break
                if data := message.get("bytes"):
                    await self._handle_frame(data)
                elif text := message.get("text"):
                    await self._handle_text(text)
        except WebSocketDisconnect:
            self.close_status = "disconnected"
            self._event("client_disconnected")
        except Exception as exc:  # noqa: BLE001 - session boundary must fail closed
            self.close_status = "error"
            self._event("session_error", error=type(exc).__name__, detail=str(exc))
            await self._safe_send_error("SESSION_ERROR", str(exc))
        finally:
            self.stopping = True
            watchdog.cancel()
            await asyncio.gather(watchdog, return_exceptions=True)
            try:
                if self.config.control.mode == "mapping":
                    self.mapping_trace.save(
                        self.recorder.session_dir / "map_candidate.json"
                    )
            finally:
                self._event("session_closed", status=self.close_status)
                self.database.close_session(
                    self.session_id, self.close_status, self.last_frame_id
                )
                self.recorder.close()

    async def _receive_hello(self) -> HelloMessage:
        raw = await asyncio.wait_for(self.websocket.receive_text(), timeout=3)
        message = parse_client_message(raw)
        if not isinstance(message, HelloMessage):
            raise TypeError("first message must be hello")
        self._validate_envelope(message.session_id, message.seq)
        if (message.target_width, message.target_height) != (
            self.config.frame.width,
            self.config.frame.height,
        ):
            raise ValueError(
                "client wire resolution does not match server configuration"
            )
        self.client_clock_offset_ms = epoch_ms() - message.sent_at_ms
        return message

    async def _handle_frame(self, packet: bytes) -> None:
        processing_started_ms = monotonic_ms()
        if len(packet) > self.config.frame.max_bytes:
            raise ValueError("frame exceeds configured size limit")
        header, image_bytes = decode_frame(packet)
        self._validate_envelope(header.session_id, header.seq)
        if header.frame_id <= self.last_frame_id:
            self._event("stale_frame", frame_id=header.frame_id)
            return
        self.last_frame_id = header.frame_id
        self.recorder.record_frame(header, image_bytes)
        frame = await asyncio.to_thread(decode_jpeg, image_bytes)
        if (frame.shape[1], frame.shape[0]) != (header.width, header.height):
            raise ValueError("frame dimensions do not match FrameHeader")
        if (header.width, header.height) != (
            self.config.frame.width,
            self.config.frame.height,
        ):
            raise ValueError("frame dimensions do not match configured wire resolution")
        translated_capture_ms = header.captured_at_ms + self.client_clock_offset_ms
        if epoch_ms() - translated_capture_ms > self.config.server.max_frame_age_ms:
            self._event("expired_frame", frame_id=header.frame_id)
            return
        perception = await asyncio.to_thread(
            self.perception.analyze,
            self.session_id,
            header.frame_id,
            header.captured_at_ms,
            frame,
        )
        self.recorder.record_perception(perception)
        world = self.world_builder.update(perception, self.decision.state)
        self.mapping_trace.add(world.minimap_player_position)
        decision = self.decision.decide(world)
        world.current_fsm_state = decision.state
        self.recorder.record_world(world)
        self.recorder.record_decision(decision)
        self.recorder.record_metric(
            "frame_processed",
            frame_id=header.frame_id,
            capture_age_ms=max(0, epoch_ms() - translated_capture_ms),
            processing_ms=monotonic_ms() - processing_started_ms,
        )

        if decision.state.value == "STOPPED":
            self.close_status = "stopped"
            await self._send_stop(decision.intent.reason)
            return
        if self.config.control.mode == "mapping" or self.pending_plan_id is not None:
            return
        plan = self.planner.plan(world, decision, self._take_server_seq())
        self.decision.commit(decision)
        self.pending_plan_id = plan.plan_id
        self.database.record_plan(plan, decision)
        await self.websocket.send_text(plan.model_dump_json())

    async def _handle_text(self, raw: str) -> None:
        message = parse_client_message(raw)
        self._validate_envelope(message.session_id, message.seq)
        if isinstance(message, HeartbeatMessage):
            await self._send_heartbeat_ack(message.sent_at_ms)
            return
        if isinstance(message, PlanAck):
            self.database.record_ack(message)
            self._event(
                "plan_ack",
                plan_id=message.plan_id,
                status=message.status.value,
                detail=message.detail,
            )
            if message.plan_id == self.pending_plan_id:
                self.pending_plan_id = None
            elif message.status != AckStatus.REJECTED:
                self._event("unexpected_plan_ack", plan_id=message.plan_id)
        elif isinstance(message, StopMessage):
            self.close_status = "client_stop"
            self._event("client_stop", reason=message.reason)
            self.stopping = True
            await self.websocket.close(code=1000, reason="client emergency stop")
        elif isinstance(message, ErrorMessage):
            self._event("client_error", code=message.code, detail=message.detail)

    def _validate_envelope(self, session_id: str, seq: int) -> None:
        if session_id != self.session_id:
            raise ValueError("session_id mismatch")
        if seq <= self.last_client_seq:
            raise ValueError(f"non-increasing client sequence: {seq}")
        self.last_client_seq = seq

    def _take_server_seq(self) -> int:
        value = self.server_seq
        self.server_seq += 1
        return value

    async def _watchdog(self) -> None:
        interval = min(0.25, self.config.server.heartbeat_timeout_ms / 4000)
        while not self.stopping:
            await asyncio.sleep(interval)
            elapsed = monotonic_ms() - self.last_seen_monotonic_ms
            if elapsed <= self.config.server.heartbeat_timeout_ms:
                continue
            self.close_status = "heartbeat_timeout"
            self._event("heartbeat_timeout", elapsed_ms=elapsed)
            await self._send_stop("server heartbeat timeout")
            try:
                await self.websocket.close(code=1011, reason="heartbeat timeout")
            except RuntimeError:
                pass
            self.stopping = True

    def _event(self, event_type: str, **fields: object) -> None:
        self.database.record_event(self.session_id, event_type, **fields)

    async def _send_stop(self, reason: str) -> None:
        message = StopMessage(
            session_id=self.session_id,
            seq=self._take_server_seq(),
            sent_at_ms=epoch_ms(),
            reason=reason,
        )
        try:
            await self.websocket.send_text(message.model_dump_json())
        except (RuntimeError, WebSocketDisconnect):
            pass

    async def _send_heartbeat_ack(self, client_sent_at_ms: int) -> None:
        received_at_ms = epoch_ms()
        response = HeartbeatAck(
            session_id=self.session_id,
            seq=self._take_server_seq(),
            server_received_at_ms=received_at_ms,
            echo_client_sent_at_ms=client_sent_at_ms,
        )
        await self.websocket.send_text(response.model_dump_json())

    async def _safe_send_error(self, code: str, detail: str) -> None:
        message = ErrorMessage(
            session_id=self.session_id,
            seq=self._take_server_seq(),
            code=code,
            detail=detail[:500],
        )
        try:
            await self.websocket.send_text(message.model_dump_json())
        except (RuntimeError, WebSocketDisconnect):
            pass


class SessionManager:
    """V1 intentionally accepts only one active client."""

    def __init__(
        self,
        config: ServerAppConfig,
        map_service: MapService,
        perception: Perception,
        database: MySQLPersistence,
    ):
        self.config = config
        self.map_service = map_service
        self.perception = perception
        self.database = database
        self._lock = asyncio.Lock()
        self._active_session_id: str | None = None

    @property
    def active_session_id(self) -> str | None:
        return self._active_session_id

    async def handle(self, websocket: WebSocket, session_id: str) -> None:
        async with self._lock:
            if self._active_session_id is not None:
                await websocket.close(code=1013, reason="V1 allows only one client")
                return
            self._active_session_id = session_id
        recorder: Recorder | None = None
        try:
            await websocket.accept()
            recorder = Recorder(self.config.recorder.root_dir, session_id, self.config)
            self.database.open_session(session_id, self.config, recorder.session_dir)
            context = SessionContext(
                session_id=session_id,
                websocket=websocket,
                config=self.config,
                map_service=self.map_service,
                perception=self.perception,
                recorder=recorder,
                database=self.database,
            )
            await context.run()
        finally:
            async with self._lock:
                self._active_session_id = None
