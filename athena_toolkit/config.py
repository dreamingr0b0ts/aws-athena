"""Configuration loading with multi-environment support.

Resolution precedence (highest wins):
    1. Explicit CLI overrides (passed to ``load_config``)
    2. Environment variables (ATHENA_REGION, ATHENA_WORKGROUP, ...)
    3. The selected ``[environments.<name>]`` table in the config file
    4. The ``[defaults]`` table in the config file
    5. Built-in fallback defaults

The config file is TOML and is searched for in this order:
    1. ``config_path`` argument / ``$ATHENA_TOOLKIT_CONFIG``
    2. ``./athena.toml``
    3. ``~/.config/athena-toolkit/config.toml``
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any

# Maps a config field -> the environment variable that can override it.
_ENV_VARS = {
    "profile": "ATHENA_PROFILE",
    "region": "ATHENA_REGION",
    "workgroup": "ATHENA_WORKGROUP",
    "output_location": "ATHENA_OUTPUT_LOCATION",
    "database": "ATHENA_DATABASE",
    "catalog": "ATHENA_CATALOG",
}

_BUILTIN_DEFAULTS: dict[str, Any] = {
    "region": "us-east-1",
    "catalog": "AwsDataCatalog",
    "poll_interval": 1.0,
    "max_wait": 300.0,
}


class ConfigError(Exception):
    """Raised when configuration cannot be resolved."""


@dataclass
class AthenaConfig:
    """Resolved settings for talking to Athena in one environment."""

    region: str = "us-east-1"
    catalog: str = "AwsDataCatalog"
    profile: str | None = None
    workgroup: str | None = None
    output_location: str | None = None
    database: str | None = None
    poll_interval: float = 1.0
    max_wait: float = 300.0
    environment: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def require_output(self) -> str:
        """Return the result location, or raise if neither it nor a workgroup is set.

        Athena needs an output location *unless* the chosen workgroup enforces
        one. We can only see the former locally, so we treat "has workgroup" as
        an acceptable substitute and let the service validate.
        """
        if not self.output_location and not self.workgroup:
            raise ConfigError(
                "No output_location and no workgroup configured. Athena needs "
                "somewhere to write results: set output_location (e.g. "
                "s3://my-bucket/results/) or a workgroup that enforces one."
            )
        return self.output_location or ""


def _config_field_names() -> set[str]:
    return {f.name for f in fields(AthenaConfig)}


def find_config_file(config_path: str | os.PathLike[str] | None = None) -> Path | None:
    """Locate the config file using the documented search order."""
    candidates: list[Path] = []
    explicit = config_path or os.environ.get("ATHENA_TOOLKIT_CONFIG")
    if explicit:
        candidates.append(Path(explicit).expanduser())
    candidates.append(Path("athena.toml"))
    candidates.append(
        Path.home() / ".config" / "athena-toolkit" / "config.toml"
    )
    for path in candidates:
        if path.is_file():
            return path
    # If the user explicitly pointed at a file that doesn't exist, that's an error.
    if explicit:
        raise ConfigError(f"Config file not found: {explicit}")
    return None


def _load_toml(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as fh:
            return tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"Failed to read config file {path}: {exc}") from exc


def load_config(
    environment: str | None = None,
    overrides: dict[str, Any] | None = None,
    config_path: str | os.PathLike[str] | None = None,
    *,
    _env: dict[str, str] | None = None,
) -> AthenaConfig:
    """Build an :class:`AthenaConfig` from file + env vars + overrides.

    Args:
        environment: Named environment to select. Falls back to
            ``$ATHENA_TOOLKIT_ENV`` then the file's ``default_environment``.
        overrides: Explicit values (typically CLI flags). ``None`` values are
            ignored so callers can pass argparse results directly.
        config_path: Optional explicit path to the TOML file.
        _env: Environment mapping (defaults to ``os.environ``); injectable for tests.
    """
    env = os.environ if _env is None else _env
    overrides = {k: v for k, v in (overrides or {}).items() if v is not None}
    valid = _config_field_names()

    file_data: dict[str, Any] = {}
    path = find_config_file(config_path)
    if path is not None:
        file_data = _load_toml(path)

    # Determine which environment to use.
    env_name = (
        environment
        or env.get("ATHENA_TOOLKIT_ENV")
        or file_data.get("default_environment")
    )

    values: dict[str, Any] = dict(_BUILTIN_DEFAULTS)

    # Layer 4: file [defaults]
    for k, v in (file_data.get("defaults") or {}).items():
        if k in valid:
            values[k] = v

    # Layer 3: file [environments.<name>]
    environments = file_data.get("environments") or {}
    if env_name:
        if env_name not in environments and environment is not None:
            # User explicitly asked for an env that isn't defined.
            raise ConfigError(
                f"Environment '{env_name}' not found in config. "
                f"Available: {', '.join(sorted(environments)) or '(none)'}"
            )
        for k, v in (environments.get(env_name) or {}).items():
            if k in valid:
                values[k] = v
        values["environment"] = env_name

    # Layer 2: environment variables
    for field_name, var in _ENV_VARS.items():
        if var in env and env[var] != "":
            values[field_name] = env[var]

    # Layer 1: explicit overrides
    for k, v in overrides.items():
        if k in valid:
            values[k] = v

    # Coerce numeric fields that may arrive as strings (env vars / CLI).
    for num_field in ("poll_interval", "max_wait"):
        if isinstance(values.get(num_field), str):
            try:
                values[num_field] = float(values[num_field])
            except ValueError as exc:
                raise ConfigError(
                    f"{num_field} must be a number, got {values[num_field]!r}"
                ) from exc

    return AthenaConfig(**{k: v for k, v in values.items() if k in valid})
