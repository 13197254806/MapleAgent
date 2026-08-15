from __future__ import annotations

import pytest

from maplebot.models import Action, ActionType, FrameHeader, decode_frame, encode_frame


def test_frame_round_trip() -> None:
    header = FrameHeader(
        session_id="s",
        seq=2,
        frame_id=1,
        captured_at_ms=100,
        width=1280,
        height=720,
        window_rect=(1, 2, 3, 4),
    )
    decoded, image = decode_frame(encode_frame(header, b"jpeg"))
    assert decoded == header
    assert image == b"jpeg"


@pytest.mark.parametrize("payload", [b"", b"\x00\x00\x00\x00", b"\x00\x01"])
def test_bad_frame_is_rejected(payload: bytes) -> None:
    with pytest.raises(ValueError):
        decode_frame(payload)


def test_mouse_action_requires_normalized_coordinates_and_button() -> None:
    action = Action(type=ActionType.MOUSE_CLICK, button="LEFT", x=0.2, y=0.8)
    assert action.button == "LEFT"
    with pytest.raises(ValueError):
        Action(type=ActionType.MOUSE_CLICK, button="LEFT")
    with pytest.raises(ValueError):
        Action(type=ActionType.MOUSE_MOVE, x=2, y=0.5)
