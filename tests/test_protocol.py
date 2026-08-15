from __future__ import annotations

import pytest

from maplebot.models import FrameHeader, decode_frame, encode_frame


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
