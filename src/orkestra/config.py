"""Persistence layer: ``~/.orkestra/config.yaml``.

The home directory defaults to ``~/.orkestra`` and can be overridden with the
``ORKESTRA_HOME`` environment variable (used by tests and for multi-profile
setups). The file is created on first save with ``0600`` permissions; the
directory with ``0700``.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import yaml
from pydantic import ValidationError as PydanticValidationError

from orkestra.errors import ConfigError
from orkestra.schema import OrkestraConfig

HOME_ENV_VAR = "ORKESTRA_HOME"
CONFIG_FILENAME = "config.yaml"


def default_home() -> Path:
    """Return the orkestra home directory.

    Honours the ``ORKESTRA_HOME`` environment variable, falling back to
    ``~/.orkestra``.
    """
    override = os.environ.get(HOME_ENV_VAR)
    if override:
        return Path(override).expanduser()
    return Path.home() / ".orkestra"


class ConfigStore:
    """Loads and saves :class:`OrkestraConfig` as YAML.

    The store owns no caching: ``load`` always re-reads the file and ``save``
    writes atomically (temp file + rename), so concurrent CLI invocations
    never observe a half-written config.
    """

    def __init__(self, home: Path | None = None) -> None:
        self._home = home or default_home()

    @property
    def home(self) -> Path:
        """The orkestra home directory in use."""
        return self._home

    @property
    def path(self) -> Path:
        """Full path of the config file."""
        return self._home / CONFIG_FILENAME

    def exists(self) -> bool:
        """Whether the config file already exists on disk."""
        return self.path.exists()

    def load(self) -> OrkestraConfig:
        """Load the config, returning an empty one when the file is absent.

        Returns:
            The parsed :class:`OrkestraConfig`.

        Raises:
            ConfigError: the file is not valid YAML, its top level is not a
                mapping, or it fails schema validation.
        """
        if not self.path.exists():
            return OrkestraConfig()
        try:
            raw = yaml.safe_load(self.path.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            raise ConfigError(f"{self.path}: not valid YAML: {exc}") from exc
        if raw is None:
            return OrkestraConfig()
        if not isinstance(raw, dict):
            raise ConfigError(f"{self.path}: top level must be a mapping, got {type(raw).__name__}")
        try:
            return OrkestraConfig.model_validate(raw)
        except PydanticValidationError as exc:
            raise ConfigError(f"{self.path}: invalid config: {exc}") from exc

    def save(self, config: OrkestraConfig) -> None:
        """Persist the config atomically with restrictive permissions.

        Args:
            config: the registry state to write.
        """
        self._home.mkdir(parents=True, exist_ok=True)
        os.chmod(self._home, 0o700)
        payload = yaml.safe_dump(
            config.model_dump(mode="json"),
            sort_keys=False,
            allow_unicode=True,
        )
        # mkstemp: unique name per writer (concurrent CLI invocations must not
        # share one temp path) and created 0600 from the start (no window in
        # which the umask could expose the file).
        fd, tmp_name = tempfile.mkstemp(
            dir=self._home, prefix=f".{CONFIG_FILENAME}.", suffix=".tmp"
        )
        tmp_path = Path(tmp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
            os.chmod(tmp_path, 0o600)
            os.replace(tmp_path, self.path)
        except BaseException:
            tmp_path.unlink(missing_ok=True)
            raise
