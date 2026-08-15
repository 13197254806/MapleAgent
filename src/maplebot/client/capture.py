from __future__ import annotations

import ctypes
import sys
import threading
from contextlib import suppress
from ctypes import wintypes
from dataclasses import dataclass

import cv2
import numpy as np

from ..clock import epoch_ms
from .window import TargetWindow


class BitmapInfoHeader(ctypes.Structure):
    _fields_ = [
        ("biSize", ctypes.c_uint32),
        ("biWidth", ctypes.c_int32),
        ("biHeight", ctypes.c_int32),
        ("biPlanes", ctypes.c_uint16),
        ("biBitCount", ctypes.c_uint16),
        ("biCompression", ctypes.c_uint32),
        ("biSizeImage", ctypes.c_uint32),
        ("biXPelsPerMeter", ctypes.c_int32),
        ("biYPelsPerMeter", ctypes.c_int32),
        ("biClrUsed", ctypes.c_uint32),
        ("biClrImportant", ctypes.c_uint32),
    ]


class BitmapInfo(ctypes.Structure):
    _fields_ = [
        ("bmiHeader", BitmapInfoHeader),
        ("bmiColors", ctypes.c_uint32 * 3),
    ]


@dataclass(frozen=True)
class CapturedFrame:
    captured_at_ms: int
    width: int
    height: int
    window_rect: tuple[int, int, int, int]
    jpeg: bytes


class WindowCapture:
    """Capture a process-owned Win32 client area without requiring foreground focus."""

    PW_CLIENTONLY = 0x00000001
    PW_RENDERFULLCONTENT = 0x00000002
    DIB_RGB_COLORS = 0
    BI_RGB = 0

    def __init__(
        self,
        target: TargetWindow,
        width: int,
        height: int,
        jpeg_quality: int,
        backend: str = "print_window",
    ):
        if sys.platform != "win32":
            raise RuntimeError("the capture client only runs on Windows")
        self.target = target
        self._target_width = width
        self._target_height = height
        self._jpeg_quality = jpeg_quality
        self._backend = backend
        self._user32 = ctypes.windll.user32
        self._gdi32 = ctypes.windll.gdi32
        self._user32.GetDC.argtypes = [wintypes.HWND]
        self._user32.GetDC.restype = wintypes.HDC
        self._user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
        self._user32.PrintWindow.argtypes = [
            wintypes.HWND,
            wintypes.HDC,
            wintypes.UINT,
        ]
        self._user32.PrintWindow.restype = wintypes.BOOL
        self._gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
        self._gdi32.CreateCompatibleDC.restype = wintypes.HDC
        self._gdi32.CreateCompatibleBitmap.argtypes = [
            wintypes.HDC,
            ctypes.c_int,
            ctypes.c_int,
        ]
        self._gdi32.CreateCompatibleBitmap.restype = wintypes.HBITMAP
        self._gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
        self._gdi32.SelectObject.restype = wintypes.HGDIOBJ
        self._gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
        self._gdi32.DeleteDC.argtypes = [wintypes.HDC]
        self._gdi32.GetDIBits.argtypes = [
            wintypes.HDC,
            wintypes.HBITMAP,
            wintypes.UINT,
            wintypes.UINT,
            wintypes.LPVOID,
            ctypes.POINTER(BitmapInfo),
            wintypes.UINT,
        ]
        self._gdi32.GetDIBits.restype = ctypes.c_int
        self._mss_factory = None
        self._mss_by_thread: dict[int, object] = {}
        if backend == "screen":
            try:
                import mss
            except ImportError as exc:  # pragma: no cover - Windows-only dependency
                raise RuntimeError(
                    "install the 'client' extra to use screen capture"
                ) from exc
            self._mss_factory = mss.mss
        try:
            self._user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
        except (AttributeError, OSError):
            with suppress(AttributeError, OSError):
                self._user32.SetProcessDPIAware()

    def capture(self) -> CapturedFrame:
        hwnd = self.target.hwnd
        window_rect = self.target.client_screen_rect()
        if self._backend == "print_window":
            frame = self._capture_window(hwnd)
        else:
            frame = self._capture_screen(window_rect)
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
            window_rect=window_rect,
            jpeg=encoded.tobytes(),
        )

    def _capture_window(self, hwnd: int) -> np.ndarray:
        width, height = self.target.client_size()
        if width <= 0 or height <= 0:
            raise RuntimeError("game window client area is empty")
        window_dc = self._user32.GetDC(hwnd)
        if not window_dc:
            raise ctypes.WinError()
        memory_dc = self._gdi32.CreateCompatibleDC(window_dc)
        bitmap = self._gdi32.CreateCompatibleBitmap(window_dc, width, height)
        if not memory_dc or not bitmap:
            if memory_dc:
                self._gdi32.DeleteDC(memory_dc)
            if bitmap:
                self._gdi32.DeleteObject(bitmap)
            self._user32.ReleaseDC(hwnd, window_dc)
            raise ctypes.WinError()
        old_object = self._gdi32.SelectObject(memory_dc, bitmap)
        try:
            flags = self.PW_CLIENTONLY | self.PW_RENDERFULLCONTENT
            if not self._user32.PrintWindow(hwnd, memory_dc, flags):
                raise RuntimeError(
                    "PrintWindow failed; this renderer may not support background capture"
                )
            info = BitmapInfo()
            info.bmiHeader.biSize = ctypes.sizeof(BitmapInfoHeader)
            info.bmiHeader.biWidth = width
            info.bmiHeader.biHeight = -height
            info.bmiHeader.biPlanes = 1
            info.bmiHeader.biBitCount = 32
            info.bmiHeader.biCompression = self.BI_RGB
            pixels = ctypes.create_string_buffer(width * height * 4)
            rows = self._gdi32.GetDIBits(
                memory_dc,
                bitmap,
                0,
                height,
                pixels,
                ctypes.byref(info),
                self.DIB_RGB_COLORS,
            )
            if rows != height:
                raise RuntimeError(f"GetDIBits returned {rows} of {height} rows")
            bgra = np.frombuffer(pixels, dtype=np.uint8).reshape(height, width, 4)
            return bgra[:, :, :3].copy()
        finally:
            self._gdi32.SelectObject(memory_dc, old_object)
            self._gdi32.DeleteObject(bitmap)
            self._gdi32.DeleteDC(memory_dc)
            self._user32.ReleaseDC(hwnd, window_dc)

    def _capture_screen(self, rect: tuple[int, int, int, int]) -> np.ndarray:
        if self.target.is_minimized():
            raise RuntimeError("screen capture cannot capture a minimized game window")
        left, top, right, bottom = rect
        if right <= left or bottom <= top:
            raise RuntimeError("game window client area is empty")
        thread_id = threading.get_ident()
        capture = self._mss_by_thread.get(thread_id)
        if capture is None:
            assert self._mss_factory is not None
            capture = self._mss_factory()
            self._mss_by_thread[thread_id] = capture
        shot = capture.grab(
            {"left": left, "top": top, "width": right - left, "height": bottom - top}
        )
        return np.asarray(shot, dtype=np.uint8)[:, :, :3]
