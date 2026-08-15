from __future__ import annotations

import numpy as np

from maplebot.config import PerceptionConfig, RoiConfig
from maplebot.perception import NoopDetector, Perception


def test_color_ui_perception() -> None:
    config = PerceptionConfig(
        rois=RoiConfig(
            minimap=(0.0, 0.0, 0.5, 0.5),
            hp=(0.0, 0.5, 0.5, 0.25),
            mp=(0.5, 0.5, 0.5, 0.25),
        )
    )
    frame = np.zeros((100, 200, 3), dtype=np.uint8)
    frame[20:24, 30:34] = (255, 255, 255)
    frame[55:70, 0:50] = (0, 0, 255)
    frame[55:70, 100:175] = (255, 0, 0)
    result = Perception(config, NoopDetector()).analyze("s", 1, 10, frame)
    assert result.minimap_player_position is not None
    assert 30 <= result.minimap_player_position.x <= 34
    assert result.hp_ratio == 0.5
    assert result.mp_ratio == 0.75
