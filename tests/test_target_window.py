from __future__ import annotations

from maplebot.client.window import _normalize_process_name


def test_process_name_normalization_accepts_exe_and_path() -> None:
    assert _normalize_process_name("Maplestory_Classic") == "maplestory_classic"
    assert _normalize_process_name("C:\\Games\\Maplestory_Classic.exe") == (
        "maplestory_classic"
    )
