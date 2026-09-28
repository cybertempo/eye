"""Test double: a module that tries to hand back demo authentication in production."""

from eye.api.auth import DemoAuth


def create_auth_port(config):  # noqa: ANN001, ANN201
    return DemoAuth.__new__(DemoAuth)
