from __future__ import annotations

import logging
from pathlib import Path

from maplebot.config import LoggingConfig
from maplebot.logging_utils import configure_server_logging


def test_server_logging_writes_rotating_file(tmp_path: Path) -> None:
    root = logging.getLogger()
    old_handlers = root.handlers[:]
    old_level = root.level
    named_loggers = [
        logging.getLogger(name)
        for name in ("uvicorn", "uvicorn.error", "uvicorn.access")
    ]
    named_state = [
        (logger.handlers[:], logger.level, logger.propagate) for logger in named_loggers
    ]
    path = tmp_path / "server.log"
    try:
        configure_server_logging(
            LoggingConfig(console=False, file=path, max_bytes=100_000)
        )
        logging.getLogger("maplebot.test").info("session=test event=ready")
        for handler in root.handlers:
            handler.flush()
        assert "session=test event=ready" in path.read_text(encoding="utf-8")
    finally:
        for handler in root.handlers:
            handler.close()
        root.handlers.clear()
        root.handlers.extend(old_handlers)
        root.setLevel(old_level)
        for logger, (handlers, level, propagate) in zip(
            named_loggers, named_state, strict=True
        ):
            logger.handlers.clear()
            logger.handlers.extend(handlers)
            logger.setLevel(level)
            logger.propagate = propagate
