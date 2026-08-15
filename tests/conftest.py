from __future__ import annotations

import json
from pathlib import Path

import pytest

from maplebot.config import ServerAppConfig


@pytest.fixture
def map_path(tmp_path: Path) -> Path:
    path = tmp_path / "map.json"
    path.write_text(
        json.dumps(
            {
                "name": "test",
                "minimap_width": 100,
                "minimap_height": 50,
                "nodes": [
                    {"id": "a", "x": 10, "y": 25, "radius": 10},
                    {"id": "b", "x": 50, "y": 25, "radius": 10},
                    {"id": "c", "x": 90, "y": 25, "radius": 10},
                ],
                "edges": [
                    {
                        "source": "a",
                        "target": "b",
                        "action": "walk_right",
                        "bidirectional": True,
                    },
                    {
                        "source": "b",
                        "target": "c",
                        "action": "walk_right",
                        "bidirectional": True,
                    },
                ],
                "patrol_route": ["a", "c"],
            }
        ),
        encoding="utf-8",
    )
    return path


@pytest.fixture
def app_config(tmp_path: Path, map_path: Path) -> ServerAppConfig:
    config = ServerAppConfig()
    config.map.path = map_path
    config.map.node_snap_distance = 12
    config.recorder.root_dir = tmp_path / "recordings"
    config.discovery.enabled = False
    config.database.enabled = False
    config.perception.detector.backend = "none"
    config.perception.smoothing_window = 3
    config.perception.player_missing_limit = 2
    return config
