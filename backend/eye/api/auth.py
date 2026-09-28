"""Authentication port.

The public repository ships only the interface and a demo implementation for
loopback use with synthetic data. The production adapter lives in the owner's
private deployment and is loaded by module name from private configuration.
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from eye.config import EyeConfig

PRIVATE_ADAPTER_FACTORY = "create_auth_port"


class AuthUnavailable(RuntimeError):
    """No acceptable authentication adapter is available; refuse to start."""


@dataclass(frozen=True)
class Principal:
    subject: str
    roles: tuple[str, ...]
    synthetic: bool


@runtime_checkable
class AuthPort(Protocol):
    name: str

    def authenticate(self, headers: dict[str, str]) -> Principal | None:
        """Return the principal for a request, or None to deny it."""
        ...


class DemoAuth:
    """Grants a fixed synthetic principal. Demo mode only; refuses production."""

    name = "demo"

    def __init__(self, mode: str) -> None:
        if mode != "demo":
            raise AuthUnavailable("demo authentication refuses to run outside demo mode")

    def authenticate(self, headers: dict[str, str]) -> Principal | None:
        return Principal(subject="synthetic-demo-user", roles=("viewer",), synthetic=True)


def resolve_auth(config: EyeConfig) -> AuthPort:
    """Return the configured adapter. Never substitutes demo auth for a missing adapter."""
    if config.mode == "demo":
        return DemoAuth(config.mode)

    module_name = config.auth.private_adapter_module
    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:
        raise AuthUnavailable(
            f"private authentication adapter {module_name!r} is not installed; "
            "production mode refuses to start"
        ) from exc
    factory = getattr(module, PRIVATE_ADAPTER_FACTORY, None)
    if not callable(factory):
        raise AuthUnavailable(f"{module_name!r} does not provide {PRIVATE_ADAPTER_FACTORY}()")
    adapter = factory(config)
    if not isinstance(adapter, AuthPort) or isinstance(adapter, DemoAuth):
        raise AuthUnavailable(f"{module_name!r} did not return a private AuthPort adapter")
    return adapter
