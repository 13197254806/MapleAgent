from __future__ import annotations

import math
from enum import StrEnum
from typing import Literal

from pydantic import Field

from .clock import epoch_ms
from .config import ControlConfig
from .map_service import MapService
from .models import (
    Action,
    ActionPlan,
    ActionType,
    EdgeAction,
    FSMState,
    WireModel,
    WorldState,
)


class IntentType(StrEnum):
    IDLE = "idle"
    MOVE_LEFT = "move_left"
    MOVE_RIGHT = "move_right"
    JUMP = "jump"
    DROP = "drop"
    ATTACK = "attack"
    USE_HP_POTION = "use_hp_potion"
    USE_MP_POTION = "use_mp_potion"
    UNSTICK = "unstick"
    STOP = "stop"


class Intent(WireModel):
    type: IntentType
    reason: str
    target_node: str | None = None
    direction: Literal["left", "right"] | None = None


class DecisionRecord(WireModel):
    session_id: str
    frame_id: int
    decided_at_ms: int = Field(default_factory=epoch_ms)
    previous_state: FSMState
    state: FSMState
    intent: Intent


class Navigator:
    def __init__(self, map_service: MapService):
        self.map = map_service
        self._route_index = 0
        self.last_direction = IntentType.MOVE_RIGHT

    def patrol(self, world: WorldState) -> Intent:
        route = self.map.model.patrol_route
        if not route:
            return Intent(type=IntentType.IDLE, reason="patrol route is empty")
        if world.player_map_node is None:
            return Intent(
                type=IntentType.IDLE, reason="player is outside known map nodes"
            )
        target = route[self._route_index % len(route)]
        if world.player_map_node == target:
            self._route_index = (self._route_index + 1) % len(route)
            target = route[self._route_index]
        edge = self.map.next_edge(world.player_map_node, target)
        if edge is None:
            return Intent(
                type=IntentType.IDLE,
                reason=f"no route from {world.player_map_node} to {target}",
                target_node=target,
            )
        intent_type = {
            EdgeAction.WALK_LEFT: IntentType.MOVE_LEFT,
            EdgeAction.WALK_RIGHT: IntentType.MOVE_RIGHT,
            EdgeAction.JUMP: IntentType.JUMP,
            EdgeAction.DROP: IntentType.DROP,
        }[edge.action]
        if intent_type in {IntentType.MOVE_LEFT, IntentType.MOVE_RIGHT}:
            self.last_direction = intent_type
        return Intent(
            type=intent_type,
            reason=f"patrol toward {target} via {edge.target}",
            target_node=target,
        )


class CombatController:
    def __init__(self, config: ControlConfig):
        self.config = config
        self._last_attack_ms = -(10**12)

    def choose(self, world: WorldState, now_ms: int) -> Intent:
        assert world.player_position is not None and world.monsters
        player = world.player_position
        monster = min(
            world.monsters,
            key=lambda item: math.hypot(
                item.position.x - player.x, item.position.y - player.y
            ),
        )
        delta_x = monster.position.x - player.x
        if abs(delta_x) > self.config.combat_range_px:
            direction = IntentType.MOVE_RIGHT if delta_x > 0 else IntentType.MOVE_LEFT
            return Intent(
                type=direction, reason=f"close distance to monster (dx={delta_x:.1f})"
            )
        if now_ms - self._last_attack_ms < self.config.attack_cooldown_ms:
            return Intent(type=IntentType.IDLE, reason="attack cooldown")
        return Intent(
            type=IntentType.ATTACK, reason=f"monster in range (dx={delta_x:.1f})"
        )

    def commit_attack(self, now_ms: int) -> None:
        self._last_attack_ms = now_ms


class DecisionEngine:
    def __init__(
        self, config: ControlConfig, map_service: MapService, player_missing_limit: int
    ):
        self.config = config
        self.navigator = Navigator(map_service)
        self.combat = CombatController(config)
        self.player_missing_limit = player_missing_limit
        self.state = FSMState.INIT
        self._unstick_attempted = False
        self._last_hp_potion_ms = -(10**12)
        self._last_mp_potion_ms = -(10**12)

    def decide(self, world: WorldState, now_ms: int | None = None) -> DecisionRecord:
        now_ms = now_ms if now_ms is not None else epoch_ms()
        previous = self.state

        if self.state == FSMState.STOPPED:
            intent = Intent(
                type=IntentType.STOP,
                reason="FSM is latched stopped; reconnect to reset",
            )
        elif world.is_dead:
            self.state = FSMState.STOPPED
            intent = Intent(type=IntentType.STOP, reason="death dialog detected")
        elif world.is_ui_blocked:
            self.state = FSMState.STOPPED
            intent = Intent(type=IntentType.STOP, reason="blocking dialog detected")
        elif self.config.mode == "mapping":
            self.state = FSMState.MAPPING
            intent = Intent(
                type=IntentType.IDLE, reason="mapping mode: automatic input disabled"
            )
        elif world.player_missing_frames >= self.player_missing_limit:
            self.state = FSMState.RECOVER
            intent = Intent(
                type=IntentType.STOP, reason="player missing for consecutive frames"
            )
        elif (
            world.hp_ratio is not None
            and world.hp_ratio <= self.config.hp_potion_threshold
        ):
            self.state = FSMState.RECOVER
            if now_ms - self._last_hp_potion_ms >= self.config.potion_cooldown_ms:
                intent = Intent(
                    type=IntentType.USE_HP_POTION,
                    reason=f"low HP ({world.hp_ratio:.2f})",
                )
            else:
                intent = Intent(type=IntentType.IDLE, reason="HP potion cooldown")
        elif (
            world.mp_ratio is not None
            and world.mp_ratio <= self.config.mp_potion_threshold
        ):
            self.state = FSMState.RECOVER
            if now_ms - self._last_mp_potion_ms >= self.config.potion_cooldown_ms:
                intent = Intent(
                    type=IntentType.USE_MP_POTION,
                    reason=f"low MP ({world.mp_ratio:.2f})",
                )
            else:
                intent = Intent(type=IntentType.IDLE, reason="MP potion cooldown")
        elif world.is_stuck and not self._unstick_attempted:
            self.state = FSMState.RECOVER
            reverse = (
                "left"
                if self.navigator.last_direction == IntentType.MOVE_RIGHT
                else "right"
            )
            intent = Intent(
                type=IntentType.UNSTICK,
                reason="position unchanged; one reverse-and-jump attempt",
                direction=reverse,
            )
        elif world.monsters and world.player_position is not None:
            self.state = FSMState.COMBAT
            self._unstick_attempted = False
            intent = self.combat.choose(world, now_ms)
        else:
            self.state = FSMState.PATROL
            if not world.is_stuck:
                self._unstick_attempted = False
            intent = self.navigator.patrol(world)

        return DecisionRecord(
            session_id=world.session_id,
            frame_id=world.frame_id,
            decided_at_ms=now_ms,
            previous_state=previous,
            state=self.state,
            intent=intent,
        )

    def commit(self, decision: DecisionRecord) -> None:
        """Apply side effects only after the corresponding plan is actually sent."""

        if decision.intent.type == IntentType.ATTACK:
            self.combat.commit_attack(decision.decided_at_ms)
        elif decision.intent.type == IntentType.USE_HP_POTION:
            self._last_hp_potion_ms = decision.decided_at_ms
        elif decision.intent.type == IntentType.USE_MP_POTION:
            self._last_mp_potion_ms = decision.decided_at_ms
        elif decision.intent.type == IntentType.UNSTICK:
            self._unstick_attempted = True


class ActionPlanner:
    def __init__(self, config: ControlConfig):
        self.config = config
        self._next_plan_id = 0

    def plan(
        self,
        world: WorldState,
        decision: DecisionRecord,
        seq: int,
        now_ms: int | None = None,
    ) -> ActionPlan:
        now_ms = now_ms if now_ms is not None else epoch_ms()
        intent = decision.intent.type
        actions: list[Action]
        if intent in {IntentType.IDLE, IntentType.STOP}:
            actions = [Action(type=ActionType.RELEASE_ALL)]
        elif intent in {IntentType.MOVE_LEFT, IntentType.MOVE_RIGHT}:
            key = "LEFT" if intent == IntentType.MOVE_LEFT else "RIGHT"
            actions = self._hold(key, self.config.walk_duration_ms)
        elif intent == IntentType.JUMP:
            actions = [Action(type=ActionType.KEY_TAP, key="JUMP")]
        elif intent == IntentType.DROP:
            actions = [
                Action(type=ActionType.KEY_DOWN, key="DOWN"),
                Action(type=ActionType.KEY_TAP, key="JUMP"),
                Action(type=ActionType.WAIT, duration_ms=100),
                Action(type=ActionType.KEY_UP, key="DOWN"),
            ]
        elif intent == IntentType.ATTACK:
            actions = [
                Action(type=ActionType.KEY_TAP, key="ATTACK"),
                Action(
                    type=ActionType.WAIT, duration_ms=self.config.attack_duration_ms
                ),
            ]
        elif intent == IntentType.USE_HP_POTION:
            actions = [Action(type=ActionType.KEY_TAP, key="HP_POTION")]
        elif intent == IntentType.USE_MP_POTION:
            actions = [Action(type=ActionType.KEY_TAP, key="MP_POTION")]
        else:  # UNSTICK: reverse once, then jump.
            key = (decision.intent.direction or "left").upper()
            actions = [
                Action(type=ActionType.KEY_DOWN, key=key),
                Action(type=ActionType.KEY_TAP, key="JUMP"),
                Action(
                    type=ActionType.WAIT, duration_ms=self.config.recovery_duration_ms
                ),
                Action(type=ActionType.KEY_UP, key=key),
            ]

        plan = ActionPlan(
            session_id=world.session_id,
            seq=seq,
            plan_id=self._next_plan_id,
            based_on_frame_id=world.frame_id,
            created_at_ms=now_ms,
            sent_at_ms=now_ms,
            ttl_ms=self.config.plan_ttl_ms,
            actions=actions,
            expected_result=decision.intent.reason,
        )
        self._next_plan_id += 1
        return plan

    @staticmethod
    def _hold(key: str, duration_ms: int) -> list[Action]:
        return [
            Action(type=ActionType.KEY_DOWN, key=key),
            Action(type=ActionType.WAIT, duration_ms=duration_ms),
            Action(type=ActionType.KEY_UP, key=key),
        ]
