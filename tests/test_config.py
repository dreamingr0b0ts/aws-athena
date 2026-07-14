import textwrap

import pytest

from athena_toolkit.config import ConfigError, load_config


CONFIG_TEXT = textwrap.dedent(
    """
    default_environment = "dev"

    [defaults]
    region = "us-east-1"
    catalog = "AwsDataCatalog"
    max_wait = 120

    [environments.dev]
    region = "us-east-1"
    workgroup = "primary"
    output_location = "s3://dev-results/"
    database = "dev_db"

    [environments.prod]
    region = "us-west-2"
    workgroup = "prod-wg"
    output_location = "s3://prod-results/"
    database = "prod_db"
    """
)


@pytest.fixture()
def config_file(tmp_path):
    path = tmp_path / "athena.toml"
    path.write_text(CONFIG_TEXT)
    return path


def test_default_environment_selected(config_file):
    cfg = load_config(config_path=config_file, _env={})
    assert cfg.environment == "dev"
    assert cfg.database == "dev_db"
    assert cfg.workgroup == "primary"
    # inherited from [defaults]
    assert cfg.max_wait == 120


def test_named_environment(config_file):
    cfg = load_config("prod", config_path=config_file, _env={})
    assert cfg.region == "us-west-2"
    assert cfg.database == "prod_db"


def test_env_var_overrides_file(config_file):
    cfg = load_config(
        "dev", config_path=config_file, _env={"ATHENA_REGION": "eu-west-1"}
    )
    assert cfg.region == "eu-west-1"


def test_cli_override_beats_env_var(config_file):
    cfg = load_config(
        "dev",
        overrides={"region": "ap-south-1"},
        config_path=config_file,
        _env={"ATHENA_REGION": "eu-west-1"},
    )
    assert cfg.region == "ap-south-1"


def test_none_overrides_ignored(config_file):
    cfg = load_config(
        "dev", overrides={"region": None}, config_path=config_file, _env={}
    )
    assert cfg.region == "us-east-1"


def test_unknown_environment_raises(config_file):
    with pytest.raises(ConfigError):
        load_config("staging", config_path=config_file, _env={})


def test_missing_explicit_config_raises(tmp_path):
    with pytest.raises(ConfigError):
        load_config(config_path=tmp_path / "nope.toml", _env={})


def test_no_config_file_uses_builtin_defaults(tmp_path, monkeypatch):
    # Run in an empty dir so ./athena.toml isn't found.
    monkeypatch.chdir(tmp_path)
    cfg = load_config(_env={})
    assert cfg.region == "us-east-1"
    assert cfg.catalog == "AwsDataCatalog"
    assert cfg.environment is None


def test_require_output_raises_without_location_or_workgroup():
    from athena_toolkit.config import AthenaConfig

    cfg = AthenaConfig()
    with pytest.raises(ConfigError):
        cfg.require_output()


def test_string_numeric_coercion(config_file):
    cfg = load_config(
        "dev",
        overrides={"max_wait": "45", "poll_interval": "2.5"},
        config_path=config_file,
        _env={},
    )
    assert cfg.max_wait == 45.0
    assert cfg.poll_interval == 2.5
