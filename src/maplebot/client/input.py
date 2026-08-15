from __future__ import annotations

import asyncio
import ctypes
import logging
import sys
from ctypes import wintypes
from typing import ClassVar, Protocol

from ..clock import epoch_ms
from ..config import InputConfig
from ..models import AckStatus, ActionPlan, ActionType
from .window import TargetWindow

LOGGER = logging.getLogger(__name__)

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

_EXTENDED_KEYS = {
    0x21,
    0x22,
    0x23,
    0x24,
    0x25,
    0x26,
    0x27,
    0x28,
    0x2D,
    0x2E,
}


class InputDevice(Protocol):
    def down(self, physical_key: str) -> None: ...

    def up(self, physical_key: str) -> None: ...

    def mouse_move(self, x: float, y: float) -> None: ...

    def mouse_down(self, button: str, x: float, y: float) -> None: ...

    def mouse_up(self, button: str, x: float, y: float) -> None: ...

    def release_all(self) -> None: ...


class Win32WindowInput:
    """Post keyboard and mouse messages only to the selected game window."""

    WM_KEYDOWN = 0x0100
    WM_KEYUP = 0x0101
    WM_MOUSEMOVE = 0x0200
    MAPVK_VK_TO_VSC = 0
    _MOUSE: ClassVar[dict[str, tuple[int, int, int]]] = {
        "LEFT": (0x0201, 0x0202, 0x0001),
        "RIGHT": (0x0204, 0x0205, 0x0002),
        "MIDDLE": (0x0207, 0x0208, 0x0010),
    }

    def __init__(self, target: TargetWindow):
        if sys.platform != "win32":
            raise RuntimeError("Windows input execution is only available on Windows")
        self.target = target
        self._user32 = ctypes.windll.user32
        self._user32.PostMessageW.argtypes = [
            wintypes.HWND,
            wintypes.UINT,
            wintypes.WPARAM,
            wintypes.LPARAM,
        ]
        self._user32.PostMessageW.restype = wintypes.BOOL
        self._user32.MapVirtualKeyW.argtypes = [wintypes.UINT, wintypes.UINT]
        self._user32.MapVirtualKeyW.restype = wintypes.UINT
        self._pressed_keys: set[int] = set()
        self._pressed_buttons: set[str] = set()
        self._last_mouse_position = (0.5, 0.5)

    def down(self, physical_key: str) -> None:
        vk = _virtual_key(physical_key)
        self._post_key(vk, is_up=False)
        self._pressed_keys.add(vk)

    def up(self, physical_key: str) -> None:
        vk = _virtual_key(physical_key)
        self._post_key(vk, is_up=True)
        self._pressed_keys.discard(vk)

    def mouse_move(self, x: float, y: float) -> None:
        self._last_mouse_position = (x, y)
        self._post_mouse(self.WM_MOUSEMOVE, x, y)

    def mouse_down(self, button: str, x: float, y: float) -> None:
        normalized = button.upper()
        down_message, _up_message, _mask = self._MOUSE[normalized]
        self._pressed_buttons.add(normalized)
        self._last_mouse_position = (x, y)
        self._post_mouse(down_message, x, y)

    def mouse_up(self, button: str, x: float, y: float) -> None:
        normalized = button.upper()
        _down_message, up_message, _mask = self._MOUSE[normalized]
        self._pressed_buttons.discard(normalized)
        self._last_mouse_position = (x, y)
        self._post_mouse(up_message, x, y)

    def release_all(self) -> None:
        for vk in tuple(self._pressed_keys):
            try:
                self._post_key(vk, is_up=True)
            except (OSError, RuntimeError) as exc:
                LOGGER.warning("failed to release virtual key %s: %s", vk, exc)
        self._pressed_keys.clear()
        x, y = self._last_mouse_position
        for button in tuple(self._pressed_buttons):
            _down_message, up_message, _mask = self._MOUSE[button]
            self._pressed_buttons.discard(button)
            try:
                self._post_mouse(up_message, x, y)
            except (OSError, RuntimeError) as exc:
                LOGGER.warning("failed to release mouse button %s: %s", button, exc)

    def _post_key(self, vk: int, is_up: bool) -> None:
        scan_code = self._user32.MapVirtualKeyW(vk, self.MAPVK_VK_TO_VSC)
        lparam = 1 | (scan_code << 16)
        if vk in _EXTENDED_KEYS:
            lparam |= 1 << 24
        if is_up:
            lparam |= (1 << 30) | (1 << 31)
        message = self.WM_KEYUP if is_up else self.WM_KEYDOWN
        if not self._user32.PostMessageW(self.target.hwnd, message, vk, lparam):
            raise ctypes.WinError()

    def _post_mouse(self, message: int, x: float, y: float) -> None:
        width, height = self.target.client_size()
        if width <= 0 or height <= 0:
            raise RuntimeError("game window client area is empty")
        client_x = min(width - 1, max(0, round(x * (width - 1))))
        client_y = min(height - 1, max(0, round(y * (height - 1))))
        lparam = (client_y << 16) | client_x
        wparam = 0
        for button in self._pressed_buttons:
            wparam |= self._MOUSE[button][2]
        if not self._user32.PostMessageW(self.target.hwnd, message, wparam, lparam):
            raise ctypes.WinError()


class ActionExecutor:
    def __init__(
        self,
        session_id: str,
        config: InputConfig,
        input_device: InputDevice,
    ):
        self.session_id = session_id
        self.config = config
        self.input = input_device
        self.last_plan_id = -1
        self.server_clock_offset_ms: float | None = None
        self._interrupted = asyncio.Event()

    async def execute(self, plan: ActionPlan) -> tuple[AckStatus, str]:
        if plan.session_id != self.session_id:
            return AckStatus.REJECTED, "session_id mismatch"
        if plan.plan_id <= self.last_plan_id:
            return AckStatus.REJECTED, "duplicate or stale plan_id"
        adjusted_now_ms = epoch_ms() + (self.server_clock_offset_ms or 0.0)
        if adjusted_now_ms > plan.created_at_ms + plan.ttl_ms:
            return AckStatus.REJECTED, "plan expired"
        allowed_keys = set(self.config.bindings)
        if any(
            action.key and action.key not in allowed_keys for action in plan.actions
        ):
            return AckStatus.REJECTED, "plan contains a non-whitelisted key"
        if any(
            action.button and action.button not in self.config.allowed_mouse_buttons
            for action in plan.actions
        ):
            return AckStatus.REJECTED, "plan contains a non-whitelisted mouse button"

        self.last_plan_id = plan.plan_id
        self._interrupted.clear()
        try:
            for action in plan.actions:
                if self._interrupted.is_set():
                    return AckStatus.INTERRUPTED, "execution interrupted"
                if action.type == ActionType.RELEASE_ALL:
                    self.input.release_all()
                elif action.type == ActionType.KEY_DOWN:
                    self.input.down(self.config.bindings[action.key])  # type: ignore[index]
                elif action.type == ActionType.KEY_UP:
                    self.input.up(self.config.bindings[action.key])  # type: ignore[index]
                elif action.type == ActionType.KEY_TAP:
                    physical = self.config.bindings[action.key]  # type: ignore[index]
                    self.input.down(physical)
                    await self._interruptible_wait(self.config.tap_duration_ms)
                    self.input.up(physical)
                elif action.type == ActionType.MOUSE_MOVE:
                    self.input.mouse_move(action.x, action.y)  # type: ignore[arg-type]
                elif action.type == ActionType.MOUSE_DOWN:
                    self.input.mouse_down(  # type: ignore[arg-type]
                        action.button, action.x, action.y
                    )
                elif action.type == ActionType.MOUSE_UP:
                    self.input.mouse_up(  # type: ignore[arg-type]
                        action.button, action.x, action.y
                    )
                elif action.type == ActionType.MOUSE_CLICK:
                    self.input.mouse_down(  # type: ignore[arg-type]
                        action.button, action.x, action.y
                    )
                    await self._interruptible_wait(self.config.mouse_tap_duration_ms)
                    self.input.mouse_up(  # type: ignore[arg-type]
                        action.button, action.x, action.y
                    )
                elif action.type == ActionType.WAIT:
                    await self._interruptible_wait(action.duration_ms or 0)
            if self._interrupted.is_set():
                return AckStatus.INTERRUPTED, "execution interrupted"
            return AckStatus.EXECUTED, ""
        except Exception:
            self.input.release_all()
            raise

    def emergency_stop(self) -> None:
        self._interrupted.set()
        self.input.release_all()

    async def _interruptible_wait(self, duration_ms: int) -> None:
        try:
            await asyncio.wait_for(self._interrupted.wait(), timeout=duration_ms / 1000)
        except TimeoutError:
            pass


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
