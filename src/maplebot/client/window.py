from __future__ import annotations

import ctypes
import sys
import threading
from ctypes import wintypes


class TargetWindow:
    """Resolve and cache the main top-level window owned by a named process."""

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    GW_OWNER = 4

    def __init__(self, process_name: str, title_substring: str | None = None):
        if sys.platform != "win32":
            raise RuntimeError("the target-window client only runs on Windows")
        self.process_name = _normalize_process_name(process_name)
        self.title_substring = title_substring.casefold() if title_substring else None
        self._user32 = ctypes.windll.user32
        self._kernel32 = ctypes.windll.kernel32
        self._user32.IsWindow.argtypes = [wintypes.HWND]
        self._user32.IsWindow.restype = wintypes.BOOL
        self._user32.IsWindowVisible.argtypes = [wintypes.HWND]
        self._user32.IsWindowVisible.restype = wintypes.BOOL
        self._user32.IsIconic.argtypes = [wintypes.HWND]
        self._user32.IsIconic.restype = wintypes.BOOL
        self._user32.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
        self._user32.GetWindow.restype = wintypes.HWND
        self._user32.GetForegroundWindow.restype = wintypes.HWND
        self._user32.GetClientRect.argtypes = [
            wintypes.HWND,
            ctypes.POINTER(wintypes.RECT),
        ]
        self._user32.GetClientRect.restype = wintypes.BOOL
        self._user32.ClientToScreen.argtypes = [
            wintypes.HWND,
            ctypes.POINTER(wintypes.POINT),
        ]
        self._user32.ClientToScreen.restype = wintypes.BOOL
        self._user32.GetWindowRect.argtypes = [
            wintypes.HWND,
            ctypes.POINTER(wintypes.RECT),
        ]
        self._user32.GetWindowRect.restype = wintypes.BOOL
        self._user32.GetWindowThreadProcessId.argtypes = [
            wintypes.HWND,
            ctypes.POINTER(wintypes.DWORD),
        ]
        self._user32.GetWindowThreadProcessId.restype = wintypes.DWORD
        self._user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
        self._user32.GetWindowTextLengthW.restype = ctypes.c_int
        self._user32.GetWindowTextW.argtypes = [
            wintypes.HWND,
            wintypes.LPWSTR,
            ctypes.c_int,
        ]
        self._user32.GetWindowTextW.restype = ctypes.c_int
        self._user32.EnumWindows.restype = wintypes.BOOL
        self._kernel32.OpenProcess.argtypes = [
            wintypes.DWORD,
            wintypes.BOOL,
            wintypes.DWORD,
        ]
        self._kernel32.OpenProcess.restype = wintypes.HANDLE
        self._kernel32.QueryFullProcessImageNameW.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            wintypes.LPWSTR,
            ctypes.POINTER(wintypes.DWORD),
        ]
        self._kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
        self._kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        self._kernel32.CloseHandle.restype = wintypes.BOOL
        self._hwnd: int | None = None
        self._lock = threading.Lock()

    @property
    def hwnd(self) -> int:
        with self._lock:
            if self._hwnd and self._matches(self._hwnd):
                return self._hwnd
            self._hwnd = self._find_main_window()
            return self._hwnd

    def is_foreground(self) -> bool:
        return self._user32.GetForegroundWindow() == self.hwnd

    def is_minimized(self) -> bool:
        return bool(self._user32.IsIconic(self.hwnd))

    def client_size(self) -> tuple[int, int]:
        rect = wintypes.RECT()
        if not self._user32.GetClientRect(self.hwnd, ctypes.byref(rect)):
            raise ctypes.WinError()
        return rect.right - rect.left, rect.bottom - rect.top

    def client_screen_rect(self) -> tuple[int, int, int, int]:
        width, height = self.client_size()
        origin = wintypes.POINT(0, 0)
        if not self._user32.ClientToScreen(self.hwnd, ctypes.byref(origin)):
            raise ctypes.WinError()
        return origin.x, origin.y, origin.x + width, origin.y + height

    def _find_main_window(self) -> int:
        matches: list[tuple[int, int]] = []
        callback_type = ctypes.WINFUNCTYPE(
            wintypes.BOOL, wintypes.HWND, wintypes.LPARAM
        )

        @callback_type
        def callback(hwnd: int, _lparam: int) -> bool:
            if not self._matches(hwnd):
                return True
            width, height = self._window_size(hwnd)
            matches.append((width * height, hwnd))
            return True

        if not self._user32.EnumWindows(callback, 0):
            raise ctypes.WinError()
        if not matches:
            title = (
                f" and title containing {self.title_substring!r}"
                if self.title_substring
                else ""
            )
            raise RuntimeError(
                f"no top-level window found for process {self.process_name!r}{title}"
            )
        return max(matches)[1]

    def _matches(self, hwnd: int) -> bool:
        if not self._user32.IsWindow(hwnd) or not self._user32.IsWindowVisible(hwnd):
            return False
        if self._user32.GetWindow(hwnd, self.GW_OWNER):
            return False
        if self.title_substring:
            title = self._window_title(hwnd)
            if self.title_substring not in title.casefold():
                return False
        pid = wintypes.DWORD()
        self._user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return bool(pid.value) and self._process_name(pid.value) == self.process_name

    def _process_name(self, pid: int) -> str | None:
        handle = self._kernel32.OpenProcess(
            self.PROCESS_QUERY_LIMITED_INFORMATION, False, pid
        )
        if not handle:
            return None
        try:
            capacity = wintypes.DWORD(32768)
            buffer = ctypes.create_unicode_buffer(capacity.value)
            if not self._kernel32.QueryFullProcessImageNameW(
                handle, 0, buffer, ctypes.byref(capacity)
            ):
                return None
            return _normalize_process_name(buffer.value)
        finally:
            self._kernel32.CloseHandle(handle)

    def _window_title(self, hwnd: int) -> str:
        length = self._user32.GetWindowTextLengthW(hwnd)
        buffer = ctypes.create_unicode_buffer(length + 1)
        self._user32.GetWindowTextW(hwnd, buffer, length + 1)
        return buffer.value

    def _window_size(self, hwnd: int) -> tuple[int, int]:
        rect = wintypes.RECT()
        if not self._user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            return 0, 0
        return max(0, rect.right - rect.left), max(0, rect.bottom - rect.top)


def _normalize_process_name(value: str) -> str:
    filename = value.replace("/", "\\").rsplit("\\", 1)[-1]
    return (
        filename[:-4] if filename.casefold().endswith(".exe") else filename
    ).casefold()
