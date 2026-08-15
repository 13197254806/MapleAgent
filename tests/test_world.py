from __future__ import annotations

from maplebot.config import ServerAppConfig
from maplebot.map_service import MapService
from maplebot.models import Box, Detection, PerceptionResult, Point
from maplebot.world import WorldStateBuilder


def perception(frame: int, x: float | None) -> PerceptionResult:
    detections = (
        []
        if x is None
        else [
            Detection(
                class_name="player",
                confidence=0.9,
                box=Box(x=x, y=10, width=10, height=10),
            )
        ]
    )
    return PerceptionResult(
        session_id="s",
        frame_id=frame,
        observed_at_ms=frame * 100,
        detections=detections,
        minimap_player_position=Point(x=10, y=25),
        confidence=0.9,
    )


def test_world_smooths_and_counts_missing(app_config: ServerAppConfig) -> None:
    service = MapService.load(app_config.map.path, app_config.map.node_snap_distance)
    builder = WorldStateBuilder(app_config.perception, service)
    builder.update(perception(1, 10))
    state = builder.update(perception(2, 30))
    assert state.player_position is not None
    assert state.player_position.x == 25  # centers are 15 and 35
    assert state.player_map_node == "a"
    assert builder.update(perception(3, None)).player_missing_frames == 1
    assert builder.update(perception(4, None)).player_missing_frames == 2
