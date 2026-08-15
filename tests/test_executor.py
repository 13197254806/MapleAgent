from __future__ import annotations

import pytest

from maplebot.client.input import ActionExecutor
from maplebot.config import InputConfig
from maplebot.models import AckStatus, Action, ActionPlan, ActionType


class FakeKeyboard:
    def __init__(self) -> None:
        self.events: list[tuple[str, str | None]] = []

    def down(self, key: str) -> None:
        self.events.append(("down", key))

    def up(self, key: str) -> None:
        self.events.append(("up", key))

    def mouse_move(self, x: float, y: float) -> None:
        self.events.append(("mouse_move", f"{x},{y}"))

    def mouse_down(self, button: str, x: float, y: float) -> None:
        self.events.append(("mouse_down", f"{button},{x},{y}"))

    def mouse_up(self, button: str, x: float, y: float) -> None:
        self.events.append(("mouse_up", f"{button},{x},{y}"))

    def release_all(self) -> None:
        self.events.append(("release_all", None))


@pytest.mark.asyncio
async def test_executor_rejects_expired_and_non_whitelist() -> None:
    keyboard = FakeKeyboard()
    executor = ActionExecutor("s", InputConfig(), keyboard)  # type: ignore[arg-type]
    expired = ActionPlan(
        session_id="s",
        seq=1,
        plan_id=1,
        based_on_frame_id=1,
        created_at_ms=1,
        ttl_ms=100,
        actions=[Action(type=ActionType.KEY_TAP, key="ATTACK")],
        expected_result="test",
    )
    assert (await executor.execute(expired))[0] == AckStatus.REJECTED

    fresh = expired.model_copy(
        update={
            "plan_id": 2,
            "created_at_ms": 9999999999999,
            "actions": [Action(type=ActionType.KEY_TAP, key="CHEAT")],
        }
    )
    assert (await executor.execute(fresh))[0] == AckStatus.REJECTED
    assert keyboard.events == []


@pytest.mark.asyncio
async def test_executor_targets_whitelisted_mouse_button() -> None:
    input_device = FakeKeyboard()
    executor = ActionExecutor(
        "s",
        InputConfig(mouse_tap_duration_ms=10),
        input_device,  # type: ignore[arg-type]
    )
    plan = ActionPlan(
        session_id="s",
        seq=1,
        plan_id=1,
        based_on_frame_id=1,
        created_at_ms=9999999999999,
        ttl_ms=100,
        actions=[Action(type=ActionType.MOUSE_CLICK, button="LEFT", x=0.25, y=0.75)],
        expected_result="click",
    )

    assert await executor.execute(plan) == (AckStatus.EXECUTED, "")
    assert input_device.events == [
        ("mouse_down", "LEFT,0.25,0.75"),
        ("mouse_up", "LEFT,0.25,0.75"),
    ]

    rejected = plan.model_copy(
        update={
            "plan_id": 2,
            "actions": [
                Action(type=ActionType.MOUSE_CLICK, button="RIGHT", x=0.5, y=0.5)
            ],
        }
    )
    assert (await executor.execute(rejected))[0] == AckStatus.REJECTED
