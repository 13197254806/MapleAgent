from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler

from .config import LoggingConfig


def configure_server_logging(config: LoggingConfig) -> None:
    """Configure one shared root logger for application and Uvicorn messages."""

    level = getattr(logging, config.level)
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    handlers: list[logging.Handler] = []
    if config.console:
        console = logging.StreamHandler()
        console.setFormatter(formatter)
        handlers.append(console)
    if config.file is not None:
        config.file.parent.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            config.file,
            maxBytes=config.max_bytes,
            backupCount=config.backup_count,
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        handlers.append(file_handler)
    if not handlers:
        handlers.append(logging.NullHandler())

    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(level)
    for handler in handlers:
        root.addHandler(handler)

    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logger = logging.getLogger(name)
        logger.handlers.clear()
        logger.propagate = True
        logger.setLevel(level)
