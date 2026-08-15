from __future__ import annotations

import pytest

from maplebot.config import DatabaseConfig, load_client_config, load_server_config


def test_split_default_configs_load_from_project() -> None:
    client = load_client_config()
    server = load_server_config()
    assert client.frame.width == server.frame.width == 1280
    assert client.discovery.service_name == server.discovery.service_name
    assert server.map.path.name == "example_map.json"
    assert server.recorder.root_dir.name == "recordings"


def test_database_password_comes_from_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = DatabaseConfig(password_env="TEST_MAPLE_MYSQL_PASSWORD")
    monkeypatch.setenv("TEST_MAPLE_MYSQL_PASSWORD", "secret")
    assert config.resolved_password() == "secret"
    assert "secret" not in config.model_dump_json()
