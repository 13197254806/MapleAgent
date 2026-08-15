from __future__ import annotations

import time


def epoch_ms() -> int:
    """Wall clock milliseconds used on the wire and in recordings."""

    return time.time_ns() // 1_000_000


def monotonic_ms() -> int:
    """Monotonic milliseconds used for local watchdogs."""

    return time.monotonic_ns() // 1_000_000
