from __future__ import annotations

from collections import deque
from statistics import median

from .config import PerceptionConfig
from .map_service import MapService
from .models import FSMState, MonsterState, PerceptionResult, Point, WorldState


class WorldStateBuilder:
    def __init__(self, config: PerceptionConfig, map_service: MapService):
        self.config = config
        self.map_service = map_service
        self._player_positions: deque[tuple[int, Point]] = deque(maxlen=300)
        self._minimap_positions: deque[Point] = deque(maxlen=config.smoothing_window)
        self._missing_frames = 0
        self._last_hp: float | None = None
        self._last_mp: float | None = None

    def update(
        self, perception: PerceptionResult, fsm_state: FSMState = FSMState.INIT
    ) -> WorldState:
        player_detections = [
            item for item in perception.detections if item.class_name == "player"
        ]
        if player_detections:
            best_player = max(player_detections, key=lambda item: item.confidence)
            self._player_positions.append(
                (perception.observed_at_ms, best_player.box.center)
            )
            self._missing_frames = 0
        else:
            self._missing_frames += 1

        if perception.minimap_player_position is not None:
            self._minimap_positions.append(perception.minimap_player_position)
        if perception.hp_ratio is not None:
            self._last_hp = perception.hp_ratio
        if perception.mp_ratio is not None:
            self._last_mp = perception.mp_ratio

        player_position = _median_point(
            [
                item[1]
                for item in list(self._player_positions)[
                    -self.config.smoothing_window :
                ]
            ]
        )
        minimap_position = _median_point(list(self._minimap_positions))
        monsters = [
            MonsterState(position=item.box.center, confidence=item.confidence)
            for item in perception.detections
            if item.class_name == "monster"
        ]
        current_node = (
            self.map_service.nearest_node(minimap_position)
            if minimap_position
            else None
        )
        return WorldState(
            session_id=perception.session_id,
            frame_id=perception.frame_id,
            observed_at_ms=perception.observed_at_ms,
            player_position=player_position,
            minimap_player_position=minimap_position,
            player_map_node=current_node,
            monsters=monsters,
            hp_ratio=self._last_hp,
            mp_ratio=self._last_mp,
            is_dead=perception.is_dead,
            is_ui_blocked=perception.is_ui_blocked,
            player_missing_frames=self._missing_frames,
            is_stuck=self._is_stuck(perception.observed_at_ms),
            perception_confidence=perception.confidence,
            current_fsm_state=fsm_state,
        )

    def _is_stuck(self, now_ms: int) -> bool:
        if len(self._player_positions) < 2:
            return False
        recent = [
            (timestamp, point)
            for timestamp, point in self._player_positions
            if now_ms - timestamp <= self.config.stuck_window_ms
        ]
        if len(recent) < 2 or now_ms - recent[0][0] < self.config.stuck_window_ms * 0.8:
            return False
        xs = [point.x for _, point in recent]
        ys = [point.y for _, point in recent]
        return (max(xs) - min(xs)) <= self.config.stuck_distance_px and (
            max(ys) - min(ys)
        ) <= self.config.stuck_distance_px


def _median_point(points: list[Point]) -> Point | None:
    if not points:
        return None
    return Point(
        x=median(point.x for point in points), y=median(point.y for point in points)
    )
