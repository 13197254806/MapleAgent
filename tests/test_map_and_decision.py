from __future__ import annotations

from maplebot.config import AppConfig
from maplebot.decision import ActionPlanner, DecisionEngine, IntentType
from maplebot.map_service import MapService
from maplebot.models import EdgeAction, FSMState, MonsterState, Point, WorldState


def world(**changes: object) -> WorldState:
    values = {
        "session_id": "s",
        "frame_id": 1,
        "observed_at_ms": 1000,
        "player_position": Point(x=100, y=100),
        "minimap_player_position": Point(x=10, y=25),
        "player_map_node": "a",
        "hp_ratio": 1.0,
        "mp_ratio": 1.0,
    }
    values.update(changes)
    return WorldState(**values)


def test_map_returns_first_path_edge(app_config: AppConfig) -> None:
    service = MapService.load(app_config.map.path, app_config.map.node_snap_distance)
    edge = service.next_edge("a", "c")
    assert edge is not None
    assert edge.source == "a"
    assert edge.action == EdgeAction.WALK_RIGHT
    reverse = service.next_edge("c", "a")
    assert reverse is not None
    assert reverse.action == EdgeAction.WALK_LEFT


def test_fsm_combat_and_stop_are_safe(app_config: AppConfig) -> None:
    service = MapService.load(app_config.map.path)
    engine = DecisionEngine(app_config.control, service, player_missing_limit=2)
    combat_world = world(
        monsters=[MonsterState(position=Point(x=120, y=100), confidence=0.9)]
    )
    combat = engine.decide(combat_world, now_ms=1000)
    assert combat.state == FSMState.COMBAT
    assert combat.intent.type == IntentType.ATTACK

    stopped = engine.decide(world(is_dead=True), now_ms=1100)
    assert stopped.state == FSMState.STOPPED
    assert stopped.intent.type == IntentType.STOP
    still_stopped = engine.decide(world(is_dead=False), now_ms=1200)
    assert still_stopped.state == FSMState.STOPPED


def test_action_plan_is_short_and_based_on_frame(app_config: AppConfig) -> None:
    service = MapService.load(app_config.map.path)
    engine = DecisionEngine(app_config.control, service, player_missing_limit=2)
    state = world()
    decision = engine.decide(state, now_ms=1000)
    plan = ActionPlanner(app_config.control).plan(state, decision, seq=7, now_ms=1000)
    assert plan.based_on_frame_id == state.frame_id
    assert 100 <= plan.ttl_ms <= 800
    assert plan.seq == 7
