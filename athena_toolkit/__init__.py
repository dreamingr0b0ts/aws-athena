"""athena-toolkit: a boto3-based CLI toolkit for AWS Athena."""

from athena_toolkit.config import AthenaConfig, load_config

__version__ = "0.1.0"

__all__ = ["AthenaConfig", "load_config", "__version__"]
