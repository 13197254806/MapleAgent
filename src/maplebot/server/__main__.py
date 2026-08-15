from __future__ import annotations

import argparse
import logging

import uvicorn

from ..config import load_server_config
from ..logging_utils import configure_server_logging
from .app import create_app

LOGGER = logging.getLogger("maplebot.server")


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the MapleBot V1 server")
    parser.add_argument(
        "--config",
        default="configs/server.yaml",
        help="server configuration (default: configs/server.yaml)",
    )
    args = parser.parse_args()
    config = load_server_config(args.config)
    configure_server_logging(config.logging)
    LOGGER.info(
        "starting server config=%s bind=%s:%s mode=%s",
        args.config,
        config.server.host,
        config.server.port,
        config.control.mode,
    )
    uvicorn.run(
        create_app(config),
        host=config.server.host,
        port=config.server.port,
        log_level=config.logging.level.lower(),
        log_config=None,
        access_log=config.logging.access_log,
    )


if __name__ == "__main__":
    main()
