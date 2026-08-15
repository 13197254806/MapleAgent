from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    field_validator,
    model_validator,
)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class FrameConfig(StrictModel):
    width: int = Field(default=1280, gt=0)
    height: int = Field(default=720, gt=0)
    max_bytes: int = Field(default=2_000_000, ge=10_000)


class ServerConfig(StrictModel):
    host: str = "0.0.0.0"
    port: int = Field(default=8765, ge=1, le=65535)
    heartbeat_timeout_ms: int = Field(default=1000, ge=250, le=10000)
    max_frame_age_ms: int = Field(default=1200, ge=100, le=10000)


class ClientConfig(StrictModel):
    # Null means discover the server automatically on the local network.
    server_url: str | None = None
    process_name: str = Field(default="Maplestory_Classic", min_length=1)
    # Optional extra filter when one process owns more than one top-level window.
    window_title: str | None = None
    capture_backend: Literal["print_window", "screen"] = "print_window"
    fps: int = Field(default=12, ge=1, le=30)
    jpeg_quality: int = Field(default=75, ge=20, le=95)
    heartbeat_interval_ms: int = Field(default=300, ge=100, le=5000)
    watchdog_timeout_ms: int = Field(default=1000, ge=250, le=10000)
    emergency_key: str = "F12"
    reconnect_delay_ms: int = Field(default=1000, ge=100, le=30000)

    @field_validator("server_url")
    @classmethod
    def websocket_url(cls, value: str | None) -> str | None:
        if value is not None and not value.startswith(("ws://", "wss://")):
            raise ValueError("client.server_url must start with ws:// or wss://")
        return value


class DiscoveryServerConfig(StrictModel):
    enabled: bool = True
    service_name: str = Field(default="maple-agent-v1", min_length=1, max_length=64)
    bind_host: str = "0.0.0.0"
    port: int = Field(default=8764, ge=1, le=65535)


class DiscoveryClientConfig(StrictModel):
    enabled: bool = True
    service_name: str = Field(default="maple-agent-v1", min_length=1, max_length=64)
    port: int = Field(default=8764, ge=1, le=65535)
    broadcast_addresses: list[str] = Field(
        default_factory=lambda: ["255.255.255.255"], min_length=1
    )
    timeout_ms: int = Field(default=500, ge=100, le=5000)
    attempts: int = Field(default=3, ge=1, le=10)


class DatabaseConfig(StrictModel):
    enabled: bool = True
    host: str = "127.0.0.1"
    port: int = Field(default=3306, ge=1, le=65535)
    user: str = "root"
    password: SecretStr | None = None
    password_env: str = "MAPLE_AGENT_MYSQL_PASSWORD"
    database: str = "maple_agent"
    connect_timeout_seconds: int = Field(default=5, ge=1, le=60)
    queue_capacity: int = Field(default=10_000, ge=100, le=100_000)

    def resolved_password(self) -> str:
        if self.password is not None:
            return self.password.get_secret_value()
        value = os.environ.get(self.password_env)
        if value:
            return value
        raise RuntimeError(
            f"database password is missing; set environment variable {self.password_env}"
        )


class InputConfig(StrictModel):
    bindings: dict[str, str] = Field(
        default_factory=lambda: {
            "LEFT": "LEFT",
            "RIGHT": "RIGHT",
            "UP": "UP",
            "DOWN": "DOWN",
            "JUMP": "SPACE",
            "ATTACK": "X",
            "HP_POTION": "1",
            "MP_POTION": "2",
        }
    )
    tap_duration_ms: int = Field(default=45, ge=10, le=200)
    mouse_tap_duration_ms: int = Field(default=45, ge=10, le=200)
    allowed_mouse_buttons: set[Literal["LEFT", "RIGHT", "MIDDLE"]] = Field(
        default_factory=lambda: {"LEFT"}
    )

    @field_validator("bindings")
    @classmethod
    def normalize_bindings(cls, value: dict[str, str]) -> dict[str, str]:
        return {key.upper(): physical.upper() for key, physical in value.items()}


class RoiConfig(StrictModel):
    # Normalized x, y, width, height in the resized frame.
    minimap: tuple[float, float, float, float] = (0.0, 0.0, 0.3, 0.3)
    hp: tuple[float, float, float, float] = (0.20, 0.94, 0.20, 0.025)
    mp: tuple[float, float, float, float] = (0.60, 0.94, 0.20, 0.025)

    @field_validator("minimap", "hp", "mp")
    @classmethod
    def normalized_roi(
        cls, value: tuple[float, float, float, float]
    ) -> tuple[float, float, float, float]:
        x, y, width, height = value
        if (
            min(value) < 0
            or width <= 0
            or height <= 0
            or x + width > 1
            or y + height > 1
        ):
            raise ValueError("ROI must be normalized and inside the frame")
        return value


class HSVRange(StrictModel):
    lower: tuple[int, int, int]
    upper: tuple[int, int, int]


class DetectorConfig(StrictModel):
    backend: Literal["none", "template", "onnx"] = "template"
    model_path: Path | None = None
    templates_dir: Path = Path("assets/templates")
    confidence_threshold: float = Field(default=0.65, ge=0, le=1)
    nms_threshold: float = Field(default=0.45, ge=0, le=1)
    input_width: int = Field(default=640, gt=0)
    input_height: int = Field(default=640, gt=0)
    class_names: list[str] = Field(
        default_factory=lambda: ["player", "monster", "death_dialog", "blocking_dialog"]
    )


class PerceptionConfig(StrictModel):
    rois: RoiConfig = Field(default_factory=RoiConfig)
    detector: DetectorConfig = Field(default_factory=DetectorConfig)
    minimap_player_hsv: HSVRange = Field(
        default_factory=lambda: HSVRange(lower=(0, 0, 225), upper=(180, 55, 255))
    )
    hp_hsv: HSVRange = Field(
        default_factory=lambda: HSVRange(lower=(0, 120, 80), upper=(12, 255, 255))
    )
    mp_hsv: HSVRange = Field(
        default_factory=lambda: HSVRange(lower=(95, 100, 70), upper=(135, 255, 255))
    )
    min_color_pixels: int = Field(default=4, ge=1)
    smoothing_window: int = Field(default=5, ge=1, le=30)
    player_missing_limit: int = Field(default=6, ge=1, le=100)
    stuck_window_ms: int = Field(default=2500, ge=500, le=30000)
    stuck_distance_px: float = Field(default=4.0, ge=0)


class ControlConfig(StrictModel):
    mode: Literal["patrol", "mapping"] = "patrol"
    plan_ttl_ms: int = Field(default=800, ge=100, le=1000)
    walk_duration_ms: int = Field(default=180, ge=20, le=700)
    attack_duration_ms: int = Field(default=220, ge=20, le=700)
    recovery_duration_ms: int = Field(default=180, ge=20, le=700)
    combat_range_px: float = Field(default=190, gt=0)
    attack_cooldown_ms: int = Field(default=450, ge=0, le=10000)
    potion_cooldown_ms: int = Field(default=1000, ge=100, le=10000)
    hp_potion_threshold: float = Field(default=0.35, ge=0, le=1)
    mp_potion_threshold: float = Field(default=0.20, ge=0, le=1)


class RecorderConfig(StrictModel):
    root_dir: Path = Path("recordings")
    save_every_nth_frame: int = Field(default=1, ge=1)


class LoggingConfig(StrictModel):
    level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    console: bool = True
    file: Path | None = Path("../logs/server.log")
    max_bytes: int = Field(default=10_000_000, ge=100_000)
    backup_count: int = Field(default=5, ge=1, le=50)
    access_log: bool = False


class MapConfig(StrictModel):
    path: Path = Path("maps/example_map.json")
    node_snap_distance: float = Field(default=16, gt=0)
    mapping_node_distance: float = Field(default=18, gt=0)


class ServerAppConfig(StrictModel):
    server: ServerConfig = Field(default_factory=ServerConfig)
    frame: FrameConfig = Field(default_factory=FrameConfig)
    discovery: DiscoveryServerConfig = Field(default_factory=DiscoveryServerConfig)
    database: DatabaseConfig = Field(default_factory=DatabaseConfig)
    perception: PerceptionConfig = Field(default_factory=PerceptionConfig)
    control: ControlConfig = Field(default_factory=ControlConfig)
    recorder: RecorderConfig = Field(default_factory=RecorderConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    map: MapConfig = Field(default_factory=MapConfig)


class ClientAppConfig(StrictModel):
    client: ClientConfig = Field(default_factory=ClientConfig)
    frame: FrameConfig = Field(default_factory=FrameConfig)
    discovery: DiscoveryClientConfig = Field(default_factory=DiscoveryClientConfig)
    input: InputConfig = Field(default_factory=InputConfig)

    @model_validator(mode="after")
    def has_endpoint_source(self) -> ClientAppConfig:
        if self.client.server_url is None and not self.discovery.enabled:
            raise ValueError(
                "enable discovery or configure client.server_url as a manual fallback"
            )
        return self


def load_server_config(path: str | Path = "configs/server.yaml") -> ServerAppConfig:
    config_path, raw = _load_yaml(path)
    config = ServerAppConfig.model_validate(raw)
    base = config_path.parent
    config.perception.detector.templates_dir = _resolve(
        base, config.perception.detector.templates_dir
    )
    if config.perception.detector.model_path:
        config.perception.detector.model_path = _resolve(
            base, config.perception.detector.model_path
        )
    config.recorder.root_dir = _resolve(base, config.recorder.root_dir)
    if config.logging.file:
        config.logging.file = _resolve(base, config.logging.file)
    config.map.path = _resolve(base, config.map.path)
    return config


def load_client_config(path: str | Path = "configs/client.yaml") -> ClientAppConfig:
    _config_path, raw = _load_yaml(path)
    return ClientAppConfig.model_validate(raw)


def _load_yaml(path: str | Path) -> tuple[Path, dict[str, object]]:
    config_path = Path(path).expanduser().resolve()
    with config_path.open("r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle) or {}
    if not isinstance(raw, dict):
        raise TypeError(f"configuration root must be a mapping: {config_path}")
    return config_path, raw


def _resolve(base: Path, path: Path) -> Path:
    return path if path.is_absolute() else (base / path).resolve()
