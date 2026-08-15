from __future__ import annotations

import ctypes
import sys
import threading
from contextlib import suppress
from dataclasses import dataclass

import cv2
import numpy as np

from ..clock import epoch_ms


@dataclass(frozen=True)
class CapturedFrame:
    captured_at_ms: int
    width: int
    height: int
    window_rect: tuple[int, int, int, int]
    jpeg: bytes


class WindowCapture:
    """Captures the visible Win32 client area and scales it to the wire resolution."""

    def __init__(
        self, title_substring: str, width: int, height: int, jpeg_quality: int
    ):
        if sys.platform != "win32":
            raise RuntimeError("the capture client only runs on Windows")
        try:
            import mss
        except ImportError as exc:  # pragma: no cover - Windows-only dependency
            raise RuntimeError(
                "install the 'client' extra to capture a window"
            ) from exc
        self._mss_factory = mss.mss
        self._mss_by_thread: dict[int, object] = {}
        self._title = title_substring.casefold()
        self._target_width = width
        self._target_height = height
        self._jpeg_quality = jpeg_quality
        self._user32 = ctypes.windll.user32
        self._hwnd: int | None = None
        try:
            self._user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
        except (AttributeError, OSError):
            with suppress(AttributeError, OSError):
                self._user32.SetProcessDPIAware()

    def capture(self) -> CapturedFrame:
        hwnd = self._find_window()
        left, top, right, bottom = self._client_rect(hwnd)
        if right <= left or bottom <= top:
            raise RuntimeError("game window client area is empty or minimized")
        thread_id = threading.get_ident()
        capture = self._mss_by_thread.get(thread_id)
        if capture is None:
            capture = self._mss_factory()
            self._mss_by_thread[thread_id] = capture
        shot = capture.grab(
            {"left": left, "top": top, "width": right - left, "height": bottom - top}
        )
        frame = np.asarray(shot, dtype=np.uint8)[:, :, :3]
        if (frame.shape[1], frame.shape[0]) != (
            self._target_width,
            self._target_height,
        ):
            frame = cv2.resize(
                frame,
                (self._target_width, self._target_height),
                interpolation=cv2.INTER_AREA,
            )
        ok, encoded = cv2.imencode(
            ".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, self._jpeg_quality]
        )
        if not ok:
            raise RuntimeError("OpenCV failed to encode the captured frame")
        return CapturedFrame(
            captured_at_ms=epoch_ms(),
            width=self._target_width,
            height=self._target_height,
            window_rect=(left, top, right, bottom),
            jpeg=encoded.tobytes(),
        )

    def _find_window(self) -> int:
        from ctypes import wintypes

        if (
            self._hwnd
            and self._user32.IsWindow(self._hwnd)
            and self._user32.IsWindowVisible(self._hwnd)
            and not self._user32.IsIconic(self._hwnd)
        ):
            return self._hwnd
        matches: list[int] = []
        callback_type = ctypes.WINFUNCTYPE(
            ctypes.c_bool, wintypes.HWND, wintypes.LPARAM
        )

        @callback_type
        def callback(hwnd: int, _lparam: int) -> bool:
            if not self._user32.IsWindowVisible(hwnd):
                return True
            if self._user32.IsIconic(hwnd):
                return True
            length = self._user32.GetWindowTextLengthW(hwnd)
            if length <= 0:
                return True
            buffer = ctypes.create_unicode_buffer(length + 1)
            self._user32.GetWindowTextW(hwnd, buffer, length + 1)
            if self._title in buffer.value.casefold():
                matches.append(hwnd)
            return True

        self._user32.EnumWindows(callback, 0)
        if not matches:
            raise RuntimeError(f"no visible window title contains {self._title!r}")
        self._hwnd = matches[0]
        return self._hwnd

    def is_game_foreground(self) -> bool:
        return (
            self._hwnd is not None and self._user32.GetForegroundWindow() == self._hwnd
        )

    def _client_rect(self, hwnd: int) -> tuple[int, int, int, int]:
        from ctypes import wintypes

        rect = wintypes.RECT()
        if not self._user32.GetClientRect(hwnd, ctypes.byref(rect)):
            raise ctypes.WinError()
        origin = wintypes.POINT(rect.left, rect.top)
        far_corner = wintypes.POINT(rect.right, rect.bottom)
        if not self._user32.ClientToScreen(hwnd, ctypes.byref(origin)):
            raise ctypes.WinError()
        if not self._user32.ClientToScreen(hwnd, ctypes.byref(far_corner)):
            raise ctypes.WinError()
        return origin.x, origin.y, far_corner.x, far_corner.y
