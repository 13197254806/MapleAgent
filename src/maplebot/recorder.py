from __future__ import annotations

import json
import re
import threading
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from .clock import epoch_ms
from .config import AppConfig
from .models import ActionPlan, FrameHeader, PerceptionResult, WorldState


class Recorder:
    STREAMS = (
        "frames",
        "perception",
        "world_state",
        "decisions",
        "action_plans",
        "events",
    )

    def __init__(self, root: Path, session_id: str, config: AppConfig):
        safe_id = re.sub(r"[^A-Za-z0-9_.-]", "_", session_id)[:80]
        self.session_dir = root / f"{epoch_ms()}-{safe_id}"
        self.frames_dir = self.session_dir / "frames"
        self.frames_dir.mkdir(parents=True, exist_ok=False)
        self._lock = threading.Lock()
        self._save_every = config.recorder.save_every_nth_frame
        self._handles = {
            name: (self.session_dir / f"{name}.jsonl").open(
                "a", encoding="utf-8", buffering=1
            )
            for name in self.STREAMS
        }
        session = {
            "schema_version": 1,
            "session_id": session_id,
            "created_at_ms": epoch_ms(),
            "config": config.model_dump(mode="json"),
        }
        (self.session_dir / "session.json").write_text(
            json.dumps(session, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def record_frame(self, header: FrameHeader, image_bytes: bytes) -> None:
        self._append("frames", header)
        if header.frame_id % self._save_every:
            return
        path = self.frames_dir / f"{header.frame_id:010d}.jpg"
        path.write_bytes(image_bytes)

    def record_perception(self, value: PerceptionResult) -> None:
        self._append("perception", value)

    def record_world(self, value: WorldState) -> None:
        self._append("world_state", value)

    def record_decision(self, value: BaseModel) -> None:
        self._append("decisions", value)

    def record_plan(self, value: ActionPlan) -> None:
        self._append("action_plans", value)

    def event(self, name: str, **fields: Any) -> None:
        self._append("events", {"at_ms": epoch_ms(), "event": name, **fields})

    def close(self) -> None:
        with self._lock:
            for handle in self._handles.values():
                handle.close()

    def _append(self, stream: str, value: BaseModel | dict[str, Any]) -> None:
        payload = (
            value.model_dump(mode="json") if isinstance(value, BaseModel) else value
        )
        line = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        with self._lock:
            self._handles[stream].write(line + "\n")


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]
