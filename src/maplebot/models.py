from __future__ import annotations

import json
import struct
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .clock import epoch_ms


class WireModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Point(WireModel):
    x: float
    y: float


class Box(WireModel):
    x: float
    y: float
    width: float = Field(gt=0)
    height: float = Field(gt=0)

    @property
    def center(self) -> Point:
        return Point(x=self.x + self.width / 2, y=self.y + self.height / 2)


class Detection(WireModel):
    class_name: Literal["player", "monster", "death_dialog", "blocking_dialog"]
    confidence: float = Field(ge=0, le=1)
    box: Box


class PerceptionResult(WireModel):
    session_id: str
    frame_id: int = Field(ge=0)
    observed_at_ms: int
    detections: list[Detection] = Field(default_factory=list)
    minimap_player_position: Point | None = None
    hp_ratio: float | None = Field(default=None, ge=0, le=1)
    mp_ratio: float | None = Field(default=None, ge=0, le=1)
    is_dead: bool = False
    is_ui_blocked: bool = False
    confidence: float = Field(default=0, ge=0, le=1)
    diagnostics: dict[str, Any] = Field(default_factory=dict)


class MonsterState(WireModel):
    position: Point
    confidence: float = Field(ge=0, le=1)


class FSMState(StrEnum):
    INIT = "INIT"
    MAPPING = "MAPPING"
    PATROL = "PATROL"
    COMBAT = "COMBAT"
    RECOVER = "RECOVER"
    STOPPED = "STOPPED"


class WorldState(WireModel):
    session_id: str
    frame_id: int = Field(ge=0)
    observed_at_ms: int
    player_position: Point | None = None
    minimap_player_position: Point | None = None
    player_map_node: str | None = None
    monsters: list[MonsterState] = Field(default_factory=list)
    hp_ratio: float | None = Field(default=None, ge=0, le=1)
    mp_ratio: float | None = Field(default=None, ge=0, le=1)
    is_dead: bool = False
    is_ui_blocked: bool = False
    player_missing_frames: int = Field(default=0, ge=0)
    is_stuck: bool = False
    perception_confidence: float = Field(default=0, ge=0, le=1)
    current_fsm_state: FSMState = FSMState.INIT


class EdgeAction(StrEnum):
    WALK_LEFT = "walk_left"
    WALK_RIGHT = "walk_right"
    JUMP = "jump"
    DROP = "drop"


class MapNode(WireModel):
    id: str
    x: float
    y: float
    radius: float = Field(default=10, gt=0)


class MapEdge(WireModel):
    source: str
    target: str
    action: EdgeAction
    bidirectional: bool = False


class MapModel(WireModel):
    name: str
    minimap_width: int = Field(gt=0)
    minimap_height: int = Field(gt=0)
    nodes: list[MapNode]
    edges: list[MapEdge]
    patrol_route: list[str]

    @field_validator("nodes")
    @classmethod
    def unique_nodes(cls, value: list[MapNode]) -> list[MapNode]:
        ids = [node.id for node in value]
        if len(ids) != len(set(ids)):
            raise ValueError("map node ids must be unique")
        return value


class ActionType(StrEnum):
    KEY_DOWN = "key_down"
    KEY_UP = "key_up"
    KEY_TAP = "key_tap"
    WAIT = "wait"
    RELEASE_ALL = "release_all"


class Action(WireModel):
    type: ActionType
    key: str | None = None
    duration_ms: int | None = Field(default=None, ge=0, le=1000)

    @field_validator("key")
    @classmethod
    def normalize_key(cls, value: str | None) -> str | None:
        return value.upper() if value else None

    def model_post_init(self, __context: Any, /) -> None:
        needs_key = self.type in {
            ActionType.KEY_DOWN,
            ActionType.KEY_UP,
            ActionType.KEY_TAP,
        }
        if needs_key and not self.key:
            raise ValueError(f"{self.type} requires key")
        if not needs_key and self.key is not None:
            raise ValueError(f"{self.type} does not accept key")
        if self.type == ActionType.WAIT and self.duration_ms is None:
            raise ValueError("wait requires duration_ms")
        if self.type != ActionType.WAIT and self.duration_ms is not None:
            raise ValueError(f"{self.type} does not accept duration_ms")


class ActionPlan(WireModel):
    type: Literal["action_plan"] = "action_plan"
    session_id: str
    seq: int = Field(ge=0)
    sent_at_ms: int = Field(default_factory=epoch_ms)
    plan_id: int = Field(ge=0)
    based_on_frame_id: int = Field(ge=0)
    created_at_ms: int = Field(default_factory=epoch_ms)
    ttl_ms: int = Field(ge=100, le=1000)
    actions: list[Action] = Field(min_length=1, max_length=16)
    expected_result: str


class HelloMessage(WireModel):
    type: Literal["hello"] = "hello"
    session_id: str
    seq: int = Field(ge=0)
    sent_at_ms: int = Field(default_factory=epoch_ms)
    client_version: str
    target_width: int = Field(gt=0)
    target_height: int = Field(gt=0)


class HeartbeatMessage(WireModel):
    type: Literal["heartbeat"] = "heartbeat"
    session_id: str
    seq: int = Field(ge=0)
    sent_at_ms: int = Field(default_factory=epoch_ms)
    last_frame_id: int = Field(ge=-1)
    last_plan_id: int = Field(ge=-1)


class HeartbeatAck(WireModel):
    type: Literal["heartbeat_ack"] = "heartbeat_ack"
    session_id: str
    seq: int = Field(ge=0)
    sent_at_ms: int = Field(default_factory=epoch_ms)
    server_received_at_ms: int = Field(default_factory=epoch_ms)
    echo_client_sent_at_ms: int


class AckStatus(StrEnum):
    EXECUTED = "executed"
    REJECTED = "rejected"
    INTERRUPTED = "interrupted"


class PlanAck(WireModel):
    type: Literal["plan_ack"] = "plan_ack"
    session_id: str
    seq: int = Field(ge=0)
    sent_at_ms: int = Field(default_factory=epoch_ms)
    plan_id: int = Field(ge=0)
    status: AckStatus
    detail: str = ""


class ErrorMessage(WireModel):
    type: Literal["error"] = "error"
    session_id: str
    seq: int = Field(ge=0)
    sent_at_ms: int = Field(default_factory=epoch_ms)
    code: str
    detail: str


class StopMessage(WireModel):
    type: Literal["stop"] = "stop"
    session_id: str
    seq: int = Field(ge=0)
    sent_at_ms: int = Field(default_factory=epoch_ms)
    reason: str


ClientMessage = Annotated[
    HelloMessage | HeartbeatMessage | PlanAck | ErrorMessage | StopMessage,
    Field(discriminator="type"),
]


class FrameHeader(WireModel):
    type: Literal["frame"] = "frame"
    session_id: str
    seq: int = Field(ge=0)
    sent_at_ms: int = Field(default_factory=epoch_ms)
    frame_id: int = Field(ge=0)
    captured_at_ms: int
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    window_rect: tuple[int, int, int, int]
    image_format: Literal["jpeg"] = "jpeg"


_HEADER_LENGTH = struct.Struct("!I")
_MAX_HEADER_BYTES = 64 * 1024


def encode_frame(header: FrameHeader, image_bytes: bytes) -> bytes:
    header_bytes = header.model_dump_json().encode("utf-8")
    if len(header_bytes) > _MAX_HEADER_BYTES:
        raise ValueError("frame header too large")
    if not image_bytes:
        raise ValueError("empty frame image")
    return _HEADER_LENGTH.pack(len(header_bytes)) + header_bytes + image_bytes


def decode_frame(packet: bytes) -> tuple[FrameHeader, bytes]:
    if len(packet) < _HEADER_LENGTH.size:
        raise ValueError("truncated frame packet")
    (header_size,) = _HEADER_LENGTH.unpack_from(packet)
    if header_size <= 0 or header_size > _MAX_HEADER_BYTES:
        raise ValueError("invalid frame header size")
    split_at = _HEADER_LENGTH.size + header_size
    if len(packet) <= split_at:
        raise ValueError("frame packet has no image")
    header = FrameHeader.model_validate_json(packet[_HEADER_LENGTH.size : split_at])
    return header, packet[split_at:]


def parse_client_message(
    raw: str,
) -> HelloMessage | HeartbeatMessage | PlanAck | ErrorMessage | StopMessage:
    payload = json.loads(raw)
    message_type = payload.get("type")
    model_by_type = {
        "hello": HelloMessage,
        "heartbeat": HeartbeatMessage,
        "plan_ack": PlanAck,
        "error": ErrorMessage,
        "stop": StopMessage,
    }
    try:
        model = model_by_type[message_type]
    except KeyError as exc:
        raise ValueError(f"unknown client message type: {message_type!r}") from exc
    return model.model_validate(payload)
