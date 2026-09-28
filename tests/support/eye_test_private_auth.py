"""Test double standing in for the private authentication adapter.

It exists only so tests can show that production mode accepts a real adapter
(positive control) while refusing a missing one. It grants nothing.
"""

from eye.api.auth import Principal


class DenyAllTestAdapter:
    name = "test-private"

    def authenticate(self, headers: dict[str, str]) -> Principal | None:
        return None


def create_auth_port(config):  # noqa: ANN001, ANN201
    return DenyAllTestAdapter()
