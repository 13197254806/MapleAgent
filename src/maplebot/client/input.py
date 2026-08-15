from __future__ import annotations

import asyncio
import ctypes
import sys
from collections.abc import Callable

from ..clock import epoch_ms
from ..config import InputConfig
from ..models import AckStatus, ActionPlan, ActionType

_SPECIAL_VK = {
    "BACKSPACE": 0x08,
    "TAB": 0x09,
    "ENTER": 0x0D,
    "SHIFT": 0x10,
    "CTRL": 0x11,
    "ALT": 0x12,
    "ESC": 0x1B,
    "SPACE": 0x20,
    "PAGEUP": 0x21,
    "PAGEDOWN": 0x22,
    "END": 0x23,
    "HOME": 0x24,
    "LEFT": 0x25,
    "UP": 0x26,
    "RIGHT": 0x27,
    "DOWN": 0x28,
    "INSERT": 0x2D,
    "DELETE": 0x2E,
    **{f"F{number}": 0x6F + number for number in range(1, 13)},
}


class Win32Keyboard:
    KEYEVENTF_KEYUP = 0x0002

    def __init__(self):
        if sys.platform != "win32":
            raise RuntimeError("Windows input execution is only available on Windows")
        self._user32 = ctypes.windll.user32
        self._pressed: set[int] = set()

    def down(self, physical_key: str) -> None:
        vk = _virtual_key(physical_key)
        self._user32.keybd_event(vk, 0, 0, 0)
        self._pressed.add(vk)

    def up(self, physical_key: str) -> None:
        vk = _virtual_key(physical_key)
        self._user32.keybd_event(vk, 0, self.KEYEVENTF_KEYUP, 0)
        self._pressed.discard(vk)

    def release_all(self) -> None:
        for vk in tuple(self._pressed):
            self._user32.keybd_event(vk, 0, self.KEYEVENTF_KEYUP, 0)
        self._pressed.clear()


class ActionExecutor:
    def __init__(
        self,
        session_id: str,
        config: InputConfig,
        keyboard: Win32Keyboard,
        foreground_check: Callable[[], bool] | None = None,
    ):
        self.session_id = session_id
        self.config = config
        self.keyboard = keyboard
        self.last_plan_id = -1
        self.server_clock_offset_ms: float | None = None
        self._interrupted = asyncio.Event()
        self._foreground_check = foreground_check

    async def execute(self, plan: ActionPlan) -> tuple[AckStatus, str]:
        if plan.session_id != self.session_id:
            return AckStatus.REJECTED, "session_id mismatch"
        if plan.plan_id <= self.last_plan_id:
            return AckStatus.REJECTED, "duplicate or stale plan_id"
        adjusted_now_ms = epoch_ms() + (self.server_clock_offset_ms or 0.0)
        if adjusted_now_ms > plan.created_at_ms + plan.ttl_ms:
            return AckStatus.REJECTED, "plan expired"
        allowed = set(self.config.bindings)
        if any(action.key and action.key not in allowed for action in plan.actions):
            return AckStatus.REJECTED, "plan contains a non-whitelisted key"
        if not self._is_safe_foreground():
            self.keyboard.release_all()
            return AckStatus.REJECTED, "game window is not foreground"

        self.last_plan_id = plan.plan_id
        self._interrupted.clear()
        try:
            for action in plan.actions:
                if self._interrupted.is_set():
                    return AckStatus.INTERRUPTED, "execution interrupted"
                if (
                    action.type != ActionType.RELEASE_ALL
                    and not self._is_safe_foreground()
                ):
                    self.emergency_stop()
                    return AckStatus.INTERRUPTED, "game window lost foreground"
                if action.type == ActionType.RELEASE_ALL:
                    self.keyboard.release_all()
                elif action.type == ActionType.KEY_DOWN:
                    self.keyboard.down(self.config.bindings[action.key])  # type: ignore[index]
                elif action.type == ActionType.KEY_UP:
                    self.keyboard.up(self.config.bindings[action.key])  # type: ignore[index]
                elif action.type == ActionType.KEY_TAP:
                    physical = self.config.bindings[action.key]  # type: ignore[index]
                    self.keyboard.down(physical)
                    await self._interruptible_wait(self.config.tap_duration_ms)
                    self.keyboard.up(physical)
                elif action.type == ActionType.WAIT:
                    await self._interruptible_wait(action.duration_ms or 0)
            if self._interrupted.is_set():
                return AckStatus.INTERRUPTED, "execution interrupted"
            return AckStatus.EXECUTED, ""
        except Exception:
            self.keyboard.release_all()
            raise

    def emergency_stop(self) -> None:
        self._interrupted.set()
        self.keyboard.release_all()

    async def _interruptible_wait(self, duration_ms: int) -> None:
        try:
            await asyncio.wait_for(self._interrupted.wait(), timeout=duration_ms / 1000)
        except TimeoutError:
            pass

    def _is_safe_foreground(self) -> bool:
        return self._foreground_check is None or self._foreground_check()


def emergency_key_pressed(key: str) -> bool:
    if sys.platform != "win32":
        return False
    vk = _virtual_key(key)
    return bool(ctypes.windll.user32.GetAsyncKeyState(vk) & 0x8000)


def _virtual_key(key: str) -> int:
    normalized = key.upper()
    if normalized in _SPECIAL_VK:
        return _SPECIAL_VK[normalized]
    if len(normalized) == 1 and normalized.isascii() and normalized.isalnum():
        return ord(normalized)
    raise ValueError(f"unsupported physical key: {key!r}")
