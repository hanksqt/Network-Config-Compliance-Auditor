"""Concurrent execution: order stability and per-device failure isolation."""

from __future__ import annotations

from dataclasses import replace

import pytest
from netmiko.exceptions import NetmikoAuthenticationException, NetmikoTimeoutException

from netauditor import runner
from netauditor.models import DeviceStatus

from .conftest import make_connector


@pytest.fixture
def devices(device):
    return [
        replace(device, name="spine1", host="172.20.20.11"),
        replace(device, name="leaf1", host="172.20.20.12"),
        replace(device, name="leaf2", host="172.20.20.13"),
    ]


def test_returns_results_in_inventory_order(devices) -> None:
    """Threads finish out of order; the report must not."""
    results = runner.run(devices, ["show version"], connector=make_connector())
    assert [r.device.name for r in results] == ["spine1", "leaf1", "leaf2"]


def test_empty_inventory_is_a_no_op() -> None:
    assert runner.run([], ["show version"]) == []


def test_uses_each_devices_backup_command(devices) -> None:
    devices[1] = replace(devices[1], backup_command="show running-config all")
    connector = make_connector()

    runner.run(devices, None, connector=connector)

    ran = sorted(cmd for conn in connector.created for cmd in conn.commands)
    assert ran == [
        "show running-config",
        "show running-config",
        "show running-config all",
    ]


def test_one_dead_device_does_not_stop_the_others(devices) -> None:
    def connector(**params):
        if params["host"] == "172.20.20.12":
            raise NetmikoTimeoutException("TCP connection to device failed.")
        return make_connector()(**params)

    results = runner.run(devices, ["show version"], connector=connector)
    statuses = {r.device.name: r.status for r in results}

    assert statuses["spine1"] is DeviceStatus.SUCCESS
    assert statuses["leaf1"] is DeviceStatus.UNREACHABLE
    assert statuses["leaf2"] is DeviceStatus.SUCCESS


def test_mixed_failure_modes_are_distinguished(devices) -> None:
    errors = {
        "172.20.20.12": NetmikoTimeoutException("TCP connection to device failed."),
        "172.20.20.13": NetmikoAuthenticationException("bad password"),
    }

    def connector(**params):
        if params["host"] in errors:
            raise errors[params["host"]]
        return make_connector()(**params)

    results = runner.run(devices, ["show version"], connector=connector)

    assert [r.status for r in results] == [
        DeviceStatus.SUCCESS,
        DeviceStatus.UNREACHABLE,
        DeviceStatus.AUTH_FAILED,
    ]


def test_progress_callback_fires_once_per_device(devices) -> None:
    seen = []
    runner.run(
        devices,
        ["show version"],
        connector=make_connector(),
        on_result=seen.append,
    )
    assert len(seen) == len(devices)


def test_worker_count_is_clamped_to_device_count(devices) -> None:
    """Asking for 64 workers for 3 devices must not spawn 64 threads."""
    results = runner.run(devices, ["show version"], workers=64, connector=make_connector())
    assert len(results) == 3
