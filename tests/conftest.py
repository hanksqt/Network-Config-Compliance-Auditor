"""Shared fixtures. Nothing here touches the network."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from netauditor.models import Credentials, Device

LAB_ENV = {
    "NETAUDIT_LAB_USERNAME": "admin",
    "NETAUDIT_LAB_PASSWORD": "admin",
}


class FakeConnection:
    """Stand-in for a Netmiko connection.

    Records what it was asked to do, and can be told to blow up on connect or
    on a specific command so the error paths are testable without a lab.
    """

    def __init__(
        self,
        *,
        outputs: dict[str, str] | None = None,
        raise_on_command: BaseException | None = None,
        **params,
    ) -> None:
        self.params = params
        self.outputs = outputs or {}
        self.raise_on_command = raise_on_command
        self.enabled = False
        self.disconnected = False
        self.commands: list[str] = []
        self.read_timeouts: list[int | None] = []

    def enable(self) -> None:
        self.enabled = True

    def send_command(self, command: str, read_timeout: int | None = None) -> str:
        self.commands.append(command)
        self.read_timeouts.append(read_timeout)
        if self.raise_on_command is not None:
            raise self.raise_on_command
        return self.outputs.get(command, f"<output of {command}>")

    def disconnect(self) -> None:
        self.disconnected = True


def make_connector(
    *,
    outputs: dict[str, str] | None = None,
    fail_times: int = 0,
    error: BaseException | None = None,
    raise_on_command: BaseException | None = None,
):
    """Build a connector factory plus a list of the connections it handed out.

    ``fail_times`` connects raise ``error`` before the first success, which is
    how the retry path gets exercised.
    """
    created: list[FakeConnection] = []
    state = {"failures_left": fail_times}

    def connector(**params) -> FakeConnection:
        if state["failures_left"] > 0:
            state["failures_left"] -= 1
            assert error is not None, "fail_times requires an error"
            raise error
        conn = FakeConnection(
            outputs=outputs, raise_on_command=raise_on_command, **params
        )
        created.append(conn)
        return conn

    connector.created = created  # type: ignore[attr-defined]
    return connector


@pytest.fixture
def credentials() -> Credentials:
    return Credentials(username="admin", password="admin")


@pytest.fixture
def device(credentials: Credentials) -> Device:
    return Device(
        name="ceos-spine1",
        host="172.20.20.11",
        device_type="arista_eos",
        credentials=credentials,
        tags=("lab", "spine"),
    )


@pytest.fixture
def write_inventory(tmp_path: Path):
    """Write a dict as inventory.yaml in a temp dir and return its path."""

    def _write(data: dict, name: str = "inventory.yaml") -> Path:
        path = tmp_path / name
        path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
        return path

    return _write


@pytest.fixture
def lab_env() -> dict[str, str]:
    return dict(LAB_ENV)
