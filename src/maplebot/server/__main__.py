from __future__ import annotations

import argparse

import uvicorn

from ..config import load_config
from .app import create_app


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the MapleBot V1 server")
    parser.add_argument(
        "--config", default="config.yaml", help="path to YAML configuration"
    )
    args = parser.parse_args()
    config = load_config(args.config)
    uvicorn.run(
        create_app(config),
        host=config.server.host,
        port=config.server.port,
        log_level="info",
    )


if __name__ == "__main__":
    main()
