from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, WebSocket

from ..config import ServerAppConfig, load_server_config
from ..discovery import DiscoveryService
from ..map_service import MapService
from ..perception import Perception
from ..persistence import MySQLPersistence
from .session import SessionManager

LOGGER = logging.getLogger(__name__)


def create_app(config: ServerAppConfig) -> FastAPI:
    map_service = MapService.load(config.map.path, config.map.node_snap_distance)
    perception = Perception(config.perception)
    database = MySQLPersistence(config.database)
    sessions = SessionManager(config, map_service, perception, database)
    discovery = DiscoveryService(config.discovery, config.server.port)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        LOGGER.info(
            "initializing server database=%s discovery=%s recordings=%s",
            "enabled" if config.database.enabled else "disabled",
            "enabled" if config.discovery.enabled else "disabled",
            config.recorder.root_dir,
        )
        await database.start()
        LOGGER.info("database state: %s", database.health)
        try:
            if config.discovery.enabled:
                await discovery.start()
            try:
                LOGGER.info("server application ready")
                yield
            finally:
                discovery.close()
        finally:
            await database.close()
            LOGGER.info("server application stopped")

    app = FastAPI(title="MapleBot V1", version="0.1.0", lifespan=lifespan)
    app.state.config = config
    app.state.sessions = sessions
    app.state.discovery = discovery
    app.state.database = database

    @app.get("/health")
    async def health() -> dict[str, object]:
        return {
            "status": "ok",
            "active_session_id": sessions.active_session_id,
            "discovery": {
                "enabled": config.discovery.enabled,
                "udp_port": config.discovery.port,
                "service_name": config.discovery.service_name,
            },
            "database": database.health,
        }

    @app.websocket("/ws/{session_id}")
    async def websocket_session(websocket: WebSocket, session_id: str) -> None:
        await sessions.handle(websocket, session_id)

    return app


def app_from_path(path: str | Path = "configs/server.yaml") -> FastAPI:
    return create_app(load_server_config(path))
