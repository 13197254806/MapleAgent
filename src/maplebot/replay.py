from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import cv2

from .clock import epoch_ms
from .config import ServerAppConfig
from .decision import ActionPlanner, DecisionEngine
from .map_service import MapService
from .models import PerceptionResult, WorldState
from .perception import Perception
from .recorder import read_jsonl
from .world import WorldStateBuilder


def replay_session(
    session_dir: str | Path, config_override: ServerAppConfig | None = None
) -> dict[str, Any]:
    session_path = Path(session_dir).expanduser().resolve()
    metadata = json.loads((session_path / "session.json").read_text(encoding="utf-8"))
    config = config_override or ServerAppConfig.model_validate(metadata["config"])
    map_service = MapService.load(config.map.path, config.map.node_snap_distance)
    perception_engine = Perception(config.perception)
    world_builder = WorldStateBuilder(config.perception, map_service)
    decision_engine = DecisionEngine(
        config.control, map_service, config.perception.player_missing_limit
    )
    planner = ActionPlanner(config.control)

    old_perceptions = _by_frame(read_jsonl(session_path / "perception.jsonl"))
    old_worlds = _by_frame(read_jsonl(session_path / "world_state.jsonl"))
    old_decisions = _by_frame(read_jsonl(session_path / "decisions.jsonl"))
    output_dir = session_path / "replays" / str(epoch_ms())
    output_dir.mkdir(parents=True, exist_ok=False)
    streams = {
        name: (output_dir / f"{name}.jsonl").open("w", encoding="utf-8")
        for name in ("perception", "world_state", "decisions", "action_plans")
    }

    metrics = {
        "frames_replayed": 0,
        "missing_recorded_frames": 0,
        "player_detection_count_changed": 0,
        "monster_count_changed": 0,
        "map_node_changed": 0,
        "fsm_state_changed": 0,
        "intent_changed": 0,
    }
    try:
        for image_path in sorted((session_path / "frames").glob("*.jpg")):
            frame_id = int(image_path.stem)
            old_perception = old_perceptions.get(frame_id)
            if old_perception is None:
                metrics["missing_recorded_frames"] += 1
                continue
            frame = cv2.imread(str(image_path), cv2.IMREAD_COLOR)
            if frame is None:
                metrics["missing_recorded_frames"] += 1
                continue
            perception = perception_engine.analyze(
                metadata["session_id"],
                frame_id,
                old_perception["observed_at_ms"],
                frame,
            )
            world = world_builder.update(perception, decision_engine.state)
            decision = decision_engine.decide(
                world, now_ms=old_decisions.get(frame_id, {}).get("decided_at_ms")
            )
            world.current_fsm_state = decision.state
            plan = planner.plan(
                world,
                decision,
                seq=metrics["frames_replayed"],
                now_ms=decision.decided_at_ms,
            )
            decision_engine.commit(decision)
            _write(streams["perception"], perception.model_dump(mode="json"))
            _write(streams["world_state"], world.model_dump(mode="json"))
            _write(streams["decisions"], decision.model_dump(mode="json"))
            _write(streams["action_plans"], plan.model_dump(mode="json"))
            metrics["frames_replayed"] += 1
            _compare(
                metrics,
                old_perception,
                old_worlds.get(frame_id),
                old_decisions.get(frame_id),
                perception,
                world,
                decision,
            )
    finally:
        for handle in streams.values():
            handle.close()

    summary = {
        **metrics,
        "source_session": str(session_path),
        "output_dir": str(output_dir),
    }
    (output_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return summary


def _by_frame(items: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
    return {int(item["frame_id"]): item for item in items if "frame_id" in item}


def _write(handle: Any, payload: dict[str, Any]) -> None:
    handle.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")


def _count_class(payload: dict[str, Any], class_name: str) -> int:
    return sum(
        item.get("class_name") == class_name for item in payload.get("detections", [])
    )


def _compare(
    metrics: dict[str, int],
    old_perception: dict[str, Any],
    old_world: dict[str, Any] | None,
    old_decision: dict[str, Any] | None,
    perception: PerceptionResult,
    world: WorldState,
    decision: Any,
) -> None:
    new_perception = perception.model_dump(mode="json")
    if _count_class(old_perception, "player") != _count_class(new_perception, "player"):
        metrics["player_detection_count_changed"] += 1
    if _count_class(old_perception, "monster") != _count_class(
        new_perception, "monster"
    ):
        metrics["monster_count_changed"] += 1
    if (
        old_world is not None
        and old_world.get("player_map_node") != world.player_map_node
    ):
        metrics["map_node_changed"] += 1
    if old_decision is not None:
        if old_decision.get("state") != decision.state.value:
            metrics["fsm_state_changed"] += 1
        if old_decision.get("intent", {}).get("type") != decision.intent.type.value:
            metrics["intent_changed"] += 1


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Replay a recorded MapleBot session offline"
    )
    parser.add_argument("session_dir", help="recorded session directory")
    args = parser.parse_args()
    print(json.dumps(replay_session(args.session_dir), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
