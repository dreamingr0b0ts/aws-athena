"""boto3 session and client construction from an :class:`AthenaConfig`."""

from __future__ import annotations

from typing import Any

import boto3

from athena_toolkit.config import AthenaConfig


def build_session(config: AthenaConfig) -> "boto3.session.Session":
    """Create a boto3 Session honouring the configured profile + region."""
    kwargs: dict[str, Any] = {"region_name": config.region}
    if config.profile:
        kwargs["profile_name"] = config.profile
    return boto3.session.Session(**kwargs)


class AwsClients:
    """Lazy holder for the AWS clients the toolkit needs.

    Clients are created on first access and cached, so constructing this is
    cheap and import-time safe (no network or credential lookup happens until
    a client is actually used).
    """

    def __init__(self, config: AthenaConfig, session: "boto3.session.Session | None" = None):
        self.config = config
        self._session = session
        self._clients: dict[str, Any] = {}

    @property
    def session(self) -> "boto3.session.Session":
        if self._session is None:
            self._session = build_session(self.config)
        return self._session

    def client(self, service: str) -> Any:
        if service not in self._clients:
            self._clients[service] = self.session.client(service)
        return self._clients[service]

    @property
    def athena(self) -> Any:
        return self.client("athena")

    @property
    def glue(self) -> Any:
        return self.client("glue")

    @property
    def s3(self) -> Any:
        return self.client("s3")
