from __future__ import annotations

import pytest
from pydantic import ValidationError

from maplebot.config import (
    ClientAppConfig,
    DiscoveryServerConfig,
)
from maplebot.discovery import (
    DiscoveryResponder,
    build_discovery_request,
    parse_discovery_offer,
)


class FakeDatagramTransport:
    def __init__(self) -> None:
        self.sent: list[tuple[bytes, tuple[str, int]]] = []

    def sendto(self, data: bytes, addr: tuple[str, int]) -> None:
        self.sent.append((data, addr))


def test_discovery_responder_returns_correlated_offer() -> None:
    config = DiscoveryServerConfig(service_name="test-service")
    responder = DiscoveryResponder(config, websocket_port=9876)
    transport = FakeDatagramTransport()
    responder.transport = transport  # type: ignore[assignment]
    nonce = "a" * 32

    responder.datagram_received(
        build_discovery_request(config.service_name, nonce), ("192.168.1.50", 32000)
    )

    assert len(transport.sent) == 1
    packet, destination = transport.sent[0]
    assert destination == ("192.168.1.50", 32000)
    offer = parse_discovery_offer(
        packet,
        source_host="192.168.1.20",
        expected_service_name=config.service_name,
        expected_nonce=nonce,
    )
    assert offer is not None
    assert offer.websocket_url == "ws://192.168.1.20:9876/ws"


def test_discovery_ignores_wrong_service_and_nonce() -> None:
    config = DiscoveryServerConfig(service_name="expected")
    responder = DiscoveryResponder(config, websocket_port=8765)
    transport = FakeDatagramTransport()
    responder.transport = transport  # type: ignore[assignment]
    responder.datagram_received(
        build_discovery_request("another-service", "a" * 32),
        ("127.0.0.1", 12345),
    )
    assert transport.sent == []


def test_config_requires_discovery_or_manual_url() -> None:
    with pytest.raises(ValidationError):
        ClientAppConfig.model_validate(
            {"client": {"server_url": None}, "discovery": {"enabled": False}}
        )
