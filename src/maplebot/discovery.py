from __future__ import annotations

import asyncio
import json
import logging
import secrets
import socket
import time
import uuid
from dataclasses import dataclass
from typing import Any

from .config import DiscoveryClientConfig, DiscoveryServerConfig

LOGGER = logging.getLogger(__name__)
PROTOCOL_VERSION = 1
DISCOVER_TYPE = "maple_agent_discover"
OFFER_TYPE = "maple_agent_offer"
MAX_PACKET_BYTES = 2048


class DiscoveryError(ConnectionError):
    pass


@dataclass(frozen=True)
class DiscoveredServer:
    host: str
    websocket_port: int
    websocket_path: str
    service_name: str
    instance_id: str

    @property
    def websocket_url(self) -> str:
        return f"ws://{self.host}:{self.websocket_port}{self.websocket_path}"


def build_discovery_request(service_name: str, nonce: str) -> bytes:
    return _encode(
        {
            "type": DISCOVER_TYPE,
            "protocol_version": PROTOCOL_VERSION,
            "service_name": service_name,
            "nonce": nonce,
        }
    )


def parse_discovery_request(data: bytes, expected_service_name: str) -> str | None:
    payload = _decode(data)
    if payload is None:
        return None
    if (
        payload.get("type") != DISCOVER_TYPE
        or payload.get("protocol_version") != PROTOCOL_VERSION
        or payload.get("service_name") != expected_service_name
    ):
        return None
    nonce = payload.get("nonce")
    if not isinstance(nonce, str) or not 16 <= len(nonce) <= 128:
        return None
    return nonce


def build_discovery_offer(
    service_name: str,
    nonce: str,
    instance_id: str,
    websocket_port: int,
    websocket_path: str = "/ws",
) -> bytes:
    return _encode(
        {
            "type": OFFER_TYPE,
            "protocol_version": PROTOCOL_VERSION,
            "service_name": service_name,
            "nonce": nonce,
            "instance_id": instance_id,
            "websocket_port": websocket_port,
            "websocket_path": websocket_path,
        }
    )


def parse_discovery_offer(
    data: bytes,
    source_host: str,
    expected_service_name: str,
    expected_nonce: str,
) -> DiscoveredServer | None:
    payload = _decode(data)
    if payload is None:
        return None
    if (
        payload.get("type") != OFFER_TYPE
        or payload.get("protocol_version") != PROTOCOL_VERSION
        or payload.get("service_name") != expected_service_name
        or payload.get("nonce") != expected_nonce
    ):
        return None
    port = payload.get("websocket_port")
    path = payload.get("websocket_path")
    instance_id = payload.get("instance_id")
    if not isinstance(port, int) or not 1 <= port <= 65535:
        return None
    if not isinstance(path, str) or not path.startswith("/") or ".." in path:
        return None
    if not isinstance(instance_id, str) or not instance_id:
        return None
    return DiscoveredServer(
        host=source_host,
        websocket_port=port,
        websocket_path=path.rstrip("/"),
        service_name=expected_service_name,
        instance_id=instance_id,
    )


def discover_server(config: DiscoveryClientConfig) -> DiscoveredServer:
    """Discover the first matching IPv4 server that replies on the LAN."""

    nonce = secrets.token_hex(16)
    request = build_discovery_request(config.service_name, nonce)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as client:
        client.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        client.bind(("", 0))
        for _attempt in range(config.attempts):
            sent = False
            for address in config.broadcast_addresses:
                try:
                    client.sendto(request, (address, config.port))
                    sent = True
                except OSError as exc:
                    LOGGER.debug("discovery broadcast to %s failed: %s", address, exc)
            if not sent:
                continue
            deadline = time.monotonic() + config.timeout_ms / 1000
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                client.settimeout(remaining)
                try:
                    data, source = client.recvfrom(MAX_PACKET_BYTES)
                except TimeoutError:
                    break
                offer = parse_discovery_offer(
                    data,
                    source_host=source[0],
                    expected_service_name=config.service_name,
                    expected_nonce=nonce,
                )
                if offer is not None:
                    return offer
    raise DiscoveryError(
        f"no '{config.service_name}' server replied on UDP {config.port}"
    )


class DiscoveryResponder(asyncio.DatagramProtocol):
    def __init__(
        self,
        config: DiscoveryServerConfig,
        websocket_port: int,
        websocket_path: str = "/ws",
    ):
        self.config = config
        self.websocket_port = websocket_port
        self.websocket_path = websocket_path
        self.instance_id = str(uuid.uuid4())
        self.transport: asyncio.DatagramTransport | None = None

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self.transport = transport  # type: ignore[assignment]
        LOGGER.info(
            "LAN discovery listening on udp://%s:%s as %s",
            self.config.bind_host,
            self.config.port,
            self.config.service_name,
        )

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        nonce = parse_discovery_request(data, self.config.service_name)
        if nonce is None or self.transport is None:
            return
        offer = build_discovery_offer(
            service_name=self.config.service_name,
            nonce=nonce,
            instance_id=self.instance_id,
            websocket_port=self.websocket_port,
            websocket_path=self.websocket_path,
        )
        self.transport.sendto(offer, addr)

    def error_received(self, exc: Exception) -> None:
        LOGGER.warning("LAN discovery UDP error: %s", exc)


class DiscoveryService:
    def __init__(self, config: DiscoveryServerConfig, websocket_port: int):
        self.config = config
        self.protocol = DiscoveryResponder(config, websocket_port)
        self._transport: asyncio.DatagramTransport | None = None

    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        transport, _protocol = await loop.create_datagram_endpoint(
            lambda: self.protocol,
            local_addr=(self.config.bind_host, self.config.port),
            allow_broadcast=True,
        )
        self._transport = transport

    def close(self) -> None:
        if self._transport is not None:
            self._transport.close()
            self._transport = None


def _encode(payload: dict[str, Any]) -> bytes:
    encoded = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    if len(encoded) > MAX_PACKET_BYTES:
        raise ValueError("discovery packet is too large")
    return encoded


def _decode(data: bytes) -> dict[str, Any] | None:
    if not data or len(data) > MAX_PACKET_BYTES:
        return None
    try:
        payload = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None
