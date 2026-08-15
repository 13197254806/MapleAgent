from __future__ import annotations

import cv2
import numpy as np
from fastapi.testclient import TestClient

from maplebot.clock import epoch_ms
from maplebot.config import AppConfig
from maplebot.models import FrameHeader, HelloMessage, encode_frame
from maplebot.replay import replay_session
from maplebot.server.app import create_app


def test_websocket_pipeline_records_and_replays(app_config: AppConfig) -> None:
    app_config.client.target_width = 160
    app_config.client.target_height = 90
    app = create_app(app_config)
    session_id = "integration"
    frame = np.zeros((90, 160, 3), dtype=np.uint8)
    ok, jpeg = cv2.imencode(".jpg", frame)
    assert ok

    with TestClient(app) as client:
        assert client.get("/health").json()["status"] == "ok"
        with client.websocket_connect(f"/ws/{session_id}") as websocket:
            hello = HelloMessage(
                session_id=session_id,
                seq=0,
                client_version="test",
                target_width=160,
                target_height=90,
            )
            websocket.send_text(hello.model_dump_json())
            assert websocket.receive_json()["type"] == "heartbeat_ack"
            header = FrameHeader(
                session_id=session_id,
                seq=1,
                frame_id=0,
                captured_at_ms=epoch_ms(),
                width=160,
                height=90,
                window_rect=(0, 0, 160, 90),
            )
            websocket.send_bytes(encode_frame(header, jpeg.tobytes()))
            assert websocket.receive_json()["type"] == "action_plan"

    sessions = list(app_config.recorder.root_dir.iterdir())
    assert len(sessions) == 1
    session_dir = sessions[0]
    assert (session_dir / "frames.jsonl").is_file()
    assert (session_dir / "frames" / "0000000000.jpg").is_file()
    summary = replay_session(session_dir)
    assert summary["frames_replayed"] == 1
    assert summary["missing_recorded_frames"] == 0
