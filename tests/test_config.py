"""Unit tests for ConfigStore: load/save, error paths, permissions."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from orkestra.config import HOME_ENV_VAR, ConfigStore, default_home
from orkestra.errors import ConfigError
from orkestra.schema import OrkestraConfig, ProviderConfig


class TestDefaultHome:
    def test_env_override(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setenv(HOME_ENV_VAR, str(tmp_path / "custom"))
        assert default_home() == tmp_path / "custom"

    def test_fallback_to_home(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(HOME_ENV_VAR, raising=False)
        assert default_home() == Path.home() / ".orkestra"


class TestLoad:
    def test_missing_file_yields_empty_config(self, store: ConfigStore) -> None:
        config = store.load()
        assert config.providers == {}
        assert config.models == {}

    def test_empty_file_yields_empty_config(self, store: ConfigStore) -> None:
        store.home.mkdir(parents=True)
        store.path.write_text("")
        assert store.load() == OrkestraConfig()

    def test_malformed_yaml_raises(self, store: ConfigStore) -> None:
        store.home.mkdir(parents=True)
        store.path.write_text("providers: [unclosed\n  bad: : :")
        with pytest.raises(ConfigError, match="not valid YAML"):
            store.load()

    def test_non_mapping_top_level_raises(self, store: ConfigStore) -> None:
        store.home.mkdir(parents=True)
        store.path.write_text("- just\n- a\n- list\n")
        with pytest.raises(ConfigError, match="top level must be a mapping"):
            store.load()

    def test_schema_violation_raises(self, store: ConfigStore) -> None:
        store.home.mkdir(parents=True)
        store.path.write_text(
            "providers:\n  p:\n    base_url: 'not-a-url'\n"
        )
        with pytest.raises(ConfigError, match="invalid config"):
            store.load()


class TestSave:
    def test_roundtrip(self, store: ConfigStore) -> None:
        config = OrkestraConfig(
            providers={
                "nova": ProviderConfig(
                    name="nova",
                    base_url="https://proxy.example.com/v1",
                    api_key_env="NOVA_KEY",
                    headers={"X-Team": "orkestra"},
                )
            }
        )
        store.save(config)
        loaded = store.load()
        assert loaded == config
        assert loaded.providers["nova"].api_key_env == "NOVA_KEY"

    def test_file_permissions(self, store: ConfigStore) -> None:
        store.save(OrkestraConfig())
        dir_mode = stat.S_IMODE(os.stat(store.home).st_mode)
        file_mode = stat.S_IMODE(os.stat(store.path).st_mode)
        assert dir_mode == 0o700
        assert file_mode == 0o600

    def test_no_secret_material_written(self, store: ConfigStore) -> None:
        config = OrkestraConfig(
            providers={
                "p": ProviderConfig(
                    name="p", base_url="https://h.test", api_key_env="MY_SECRET_ENV"
                )
            }
        )
        store.save(config)
        contents = store.path.read_text()
        assert "MY_SECRET_ENV" in contents  # env var *name* is stored
        # and nothing else that looks like a key value snuck in
        assert "api_key" not in contents.replace("api_key_env", "")

    def test_no_tmp_file_left(self, store: ConfigStore) -> None:
        store.save(OrkestraConfig())
        leftovers = list(store.home.glob("*.tmp"))
        assert leftovers == []

    def test_concurrent_saves_do_not_collide(self, store: ConfigStore) -> None:
        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(lambda _: store.save(OrkestraConfig()), range(32)))
        assert store.load() == OrkestraConfig()
        assert list(store.home.glob("*.tmp")) == []

    def test_failed_replace_cleans_tmp(
        self, store: ConfigStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def boom(*_: object) -> None:
            raise OSError("replace failed")

        monkeypatch.setattr("orkestra.config.os.replace", boom)
        with pytest.raises(OSError):
            store.save(OrkestraConfig())
        assert list(store.home.glob("*.tmp")) == []
