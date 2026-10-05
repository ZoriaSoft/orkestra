"""Shared pytest fixtures: isolated config home + mock OpenAI server."""

from __future__ import annotations

import socket
from collections.abc import Iterator
from pathlib import Path

import pytest
from typer.testing import CliRunner

from orkestra.config import HOME_ENV_VAR, ConfigStore
from orkestra.schema import ProviderConfig

from .mock_openai import MockOpenAIServer

TEST_KEY_ENV = "ORKESTRA_TEST_API_KEY"
TEST_KEY_VALUE = "test-secret-value"


@pytest.fixture(autouse=True)
def _wide_console(monkeypatch: pytest.MonkeyPatch) -> None:
    """Wide terminal so rich tables don't ellipsize values under CliRunner."""
    monkeypatch.setenv("COLUMNS", "200")


@pytest.fixture
def orkestra_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point ORKESTRA_HOME at a throwaway dir so tests never touch ~/.orkestra."""
    home = tmp_path / "orkestra-home"
    monkeypatch.setenv(HOME_ENV_VAR, str(home))
    return home


@pytest.fixture
def store(orkestra_home: Path) -> ConfigStore:
    """A ConfigStore rooted at the isolated home."""
    return ConfigStore()


@pytest.fixture
def api_key_env(monkeypatch: pytest.MonkeyPatch) -> str:
    """Provide the env var referenced by test providers."""
    monkeypatch.setenv(TEST_KEY_ENV, TEST_KEY_VALUE)
    return TEST_KEY_ENV


@pytest.fixture
def mock_server() -> Iterator[MockOpenAIServer]:
    """A running mock OpenAI-compatible server."""
    server = MockOpenAIServer().start()
    yield server
    server.stop()


@pytest.fixture
def mock_provider(mock_server: MockOpenAIServer) -> ProviderConfig:
    """ProviderConfig wired to the mock server."""
    return ProviderConfig(
        name="mock",
        base_url=mock_server.base_url,
        api_key_env=TEST_KEY_ENV,
    )


@pytest.fixture
def runner() -> CliRunner:
    """typer's CLI test runner."""
    return CliRunner()


def unused_port() -> int:
    """A localhost port that is closed right now (for unreachable tests)."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def combined_output(result: object) -> str:
    """stdout + stderr of a CliRunner result, for message assertions."""
    stdout = getattr(result, "output", "") or ""
    stderr = getattr(result, "stderr", "") or ""
    return stdout + stderr
