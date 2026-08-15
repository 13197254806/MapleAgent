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
