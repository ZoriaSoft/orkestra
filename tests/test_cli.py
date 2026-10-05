"""End-to-end CLI tests through typer's CliRunner.

Each test runs the real ``orkestra`` app against an isolated ORKESTRA_HOME
and, for provider tests, a real local mock OpenAI server.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from orkestra.cli.main import app

from .conftest import TEST_KEY_ENV, TEST_KEY_VALUE, combined_output, unused_port
from .mock_openai import MockOpenAIServer


def _add_provider(
    runner: CliRunner,
    base_url: str,
    name: str = "mock",
    *extra: str,
) -> object:
    return runner.invoke(
        app,
        [
            "providers", "add", name,
            "--base-url", base_url,
            "--api-key-env", TEST_KEY_ENV,
            *extra,
        ],
    )


@pytest.mark.usefixtures("orkestra_home")
class TestProvidersCli:
    def test_add_and_list(self, runner: CliRunner, mock_server: MockOpenAIServer) -> None:
        result = _add_provider(runner, mock_server.v1_url)
        assert result.exit_code == 0, result.output
        assert "added" in result.output

        result = runner.invoke(app, ["providers", "list"])
        assert result.exit_code == 0
        assert "mock" in result.output
        assert mock_server.v1_url in result.output

    def test_add_duplicate_fails(
        self, runner: CliRunner, mock_server: MockOpenAIServer
    ) -> None:
        _add_provider(runner, mock_server.base_url)
        result = _add_provider(runner, mock_server.base_url)
        assert result.exit_code == 1
        assert "already exists" in combined_output(result)

    def test_add_invalid_url_fails(self, runner: CliRunner) -> None:
        result = _add_provider(runner, "not-a-url")
        assert result.exit_code == 1
        assert "http" in combined_output(result)

    def test_add_invalid_name_fails(self, runner: CliRunner) -> None:
        result = _add_provider(runner, "https://h.test", "Bad Name")
        assert result.exit_code == 1
        assert "invalid provider name" in combined_output(result)

    def test_add_warns_when_env_missing(
        self,
        runner: CliRunner,
        mock_server: MockOpenAIServer,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv(TEST_KEY_ENV, raising=False)
        result = _add_provider(runner, mock_server.base_url)
        assert result.exit_code == 0
        assert TEST_KEY_ENV in combined_output(result)

    def test_test_command_ok(
        self,
        runner: CliRunner,
        mock_server: MockOpenAIServer,
        api_key_env: str,
    ) -> None:
        mock_server.require_key = TEST_KEY_VALUE
        _add_provider(runner, mock_server.v1_url)
        result = runner.invoke(app, ["providers", "test", "mock"])
        assert result.exit_code == 0, result.output
        assert "ms" in result.output
        assert str(len(mock_server.models)) in result.output
        for model_id in mock_server.models:
            assert model_id in result.output

    def test_test_command_missing_key(
        self,
        runner: CliRunner,
        mock_server: MockOpenAIServer,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv(TEST_KEY_ENV, raising=False)
        _add_provider(runner, mock_server.base_url)
        result = runner.invoke(app, ["providers", "test", "mock"])
        assert result.exit_code == 1
        assert TEST_KEY_ENV in combined_output(result)

    def test_test_command_unreachable(
        self, runner: CliRunner, api_key_env: str
    ) -> None:
        _add_provider(runner, f"http://127.0.0.1:{unused_port()}")
        result = runner.invoke(app, ["providers", "test", "mock"])
        assert result.exit_code == 1
        assert "error" in combined_output(result)

    def test_test_unknown_provider(self, runner: CliRunner) -> None:
        result = runner.invoke(app, ["providers", "test", "ghost"])
        assert result.exit_code == 1
        assert "not found" in combined_output(result)

    def test_remove(
        self, runner: CliRunner, mock_server: MockOpenAIServer
    ) -> None:
        _add_provider(runner, mock_server.base_url)
        result = runner.invoke(app, ["providers", "remove", "mock"])
        assert result.exit_code == 0
        result = runner.invoke(app, ["providers", "list"])
        assert "mock" not in result.output

    def test_no_secrets_in_config_file(
        self,
        runner: CliRunner,
        mock_server: MockOpenAIServer,
        orkestra_home: Path,
        api_key_env: str,
    ) -> None:
        _add_provider(runner, mock_server.base_url)
        contents = (orkestra_home / "config.yaml").read_text()
        assert TEST_KEY_VALUE not in contents
        assert TEST_KEY_ENV in contents


@pytest.mark.usefixtures("orkestra_home")
class TestModelsCli:
    def _seed_provider(self, runner: CliRunner, mock_server: MockOpenAIServer) -> None:
        assert _add_provider(runner, mock_server.base_url).exit_code == 0

    def test_add_and_list(
        self, runner: CliRunner, mock_server: MockOpenAIServer
    ) -> None:
        self._seed_provider(runner, mock_server)
        result = runner.invoke(
            app,
            [
                "models", "add", "hamal-1",
                "--provider", "mock",
                "--tier", "cheap",
                "--purpose", "micro-task",
                "--cost-in", "0.10",
                "--cost-out", "0.40",
            ],
        )
        assert result.exit_code == 0, combined_output(result)

        result = runner.invoke(app, ["models", "list"])
        assert result.exit_code == 0
        assert "hamal-1" in result.output
        assert "cheap" in result.output
        assert "micro-task" in result.output
        assert "0.1" in result.output

    def test_list_tier_filter(
        self, runner: CliRunner, mock_server: MockOpenAIServer
    ) -> None:
        self._seed_provider(runner, mock_server)
        runner.invoke(app, ["models", "add", "cheap-1", "-p", "mock", "-t", "cheap"])
        runner.invoke(app, ["models", "add", "strong-1", "-p", "mock", "-t", "strong"])
        result = runner.invoke(app, ["models", "list", "--tier", "cheap"])
        assert "cheap-1" in result.output
        assert "strong-1" not in result.output

    def test_add_unknown_provider_fails(self, runner: CliRunner) -> None:
        result = runner.invoke(
            app, ["models", "add", "m", "--provider", "ghost", "--tier", "cheap"]
        )
        assert result.exit_code == 1
        assert "not found" in combined_output(result)

    def test_remove(
        self, runner: CliRunner, mock_server: MockOpenAIServer
    ) -> None:
        self._seed_provider(runner, mock_server)
        runner.invoke(app, ["models", "add", "m1", "-p", "mock", "-t", "cheap"])
        result = runner.invoke(app, ["models", "remove", "m1"])
        assert result.exit_code == 0
        result = runner.invoke(app, ["models", "list"])
        assert "m1" not in result.output

    def test_provider_remove_blocked_by_model(
        self, runner: CliRunner, mock_server: MockOpenAIServer
    ) -> None:
        self._seed_provider(runner, mock_server)
        runner.invoke(app, ["models", "add", "m1", "-p", "mock", "-t", "cheap"])
        result = runner.invoke(app, ["providers", "remove", "mock"])
        assert result.exit_code == 1
        assert "m1" in combined_output(result)


@pytest.mark.usefixtures("orkestra_home")
class TestConfigCli:
    def test_path(self, runner: CliRunner, orkestra_home: Path) -> None:
        result = runner.invoke(app, ["config", "path"])
        assert result.exit_code == 0
        assert str(orkestra_home) in result.output

    def test_show(self, runner: CliRunner, mock_server: MockOpenAIServer) -> None:
        _add_provider(runner, mock_server.base_url)
        result = runner.invoke(app, ["config", "show"])
        assert result.exit_code == 0
        assert "mock" in result.output
        assert TEST_KEY_ENV in result.output


class TestMisc:
    def test_version(self, runner: CliRunner) -> None:
        result = runner.invoke(app, ["--version"])
        assert result.exit_code == 0
        assert "orkestra" in result.output

    def test_no_args_shows_help(self, runner: CliRunner) -> None:
        result = runner.invoke(app, [])
        assert result.exit_code in (0, 2)
        assert "providers" in result.output
