from __future__ import annotations

import argparse
import asyncio
import logging

from ..config import load_config
from .runtime import ClientRuntime


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the MapleBot V1 Windows client")
    parser.add_argument(
        "--config", default="config.yaml", help="path to YAML configuration"
    )
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    asyncio.run(ClientRuntime(load_config(args.config)).run())


if __name__ == "__main__":
    main()
