"""Error classification and the collect() contract.

The point of these tests: one broken device must never take down a run, and the
failure must be reported as something a human can act on.
"""

from __future__ import annotations

import socket
from dataclasses import replace

import pytest
from netmiko.exceptions import (
    NetmikoAuthenticationException,
    NetmikoTimeoutException,
    ReadTimeout,
)
from paramiko.ssh_exception import SSHException

from netauditor import connect
from netauditor.models import DeviceStatus

from .conftest import make_connector


class TestClassifyException:
    @pytest.mark.parametrize(
        ("exc", "expected"),
        [
            (NetmikoAuthenticationException("bad password"), DeviceStatus.AUTH_FAILED),
            (
                NetmikoTimeoutException("TCP connection to device failed."),
                DeviceStatus.UNREACHABLE,
            ),
            (
                NetmikoTimeoutException("Timed-out reading channel, data not available."),
                DeviceStatus.TIMEOUT,
            ),
            (ReadTimeout("pattern never detected"), DeviceStatus.COMMAND_FAILED),
            (socket.gaierror("Name or service not known"), DeviceStatus.UNREACHABLE),
            (ConnectionRefusedError("refused"), DeviceStatus.UNREACHABLE),
            (SSHException("negotiation failed"), DeviceStatus.ERROR),
            (ValueError("Unsupported device_type: nonsense"), DeviceStatus.CONFIG_ERROR),
            (RuntimeError("something else"), DeviceStatus.ERROR),
        ],
    )
    def test_status_mapping(self, exc: BaseException, expected: DeviceStatus) -> None:
        status, _ = connect.classify_exception(exc)
        assert status == expected

    def test_reason_is_a_single_line(self) -> None:
        _, reason = connect.classify_exception(RuntimeError("line one\nline two"))
        assert "\n" not in reason
        assert "line two" not in reason

    def test_reason_is_non_empty_for_bare_exception(self) -> None:
        _, reason = connect.classify_exception(RuntimeError())
        assert reason.strip()


class TestCollectSuccess:
    def test_returns_command_output(self, device) -> None:
        connector = make_connector(outputs={"show version": "vEOS 4.32"})
        result = connect.collect(device, ["show version"], connector=connector)

        assert result.status is DeviceStatus.SUCCESS
        assert result.ok
        assert result.outputs == {"show version": "vEOS 4.32"}
        assert result.attempts == 1
        assert result.error is None

    def test_runs_every_command_in_one_session(self, device) -> None:
        connector = make_connector()
        connect.collect(device, ["show version", "show running-config"], connector=connector)

        assert len(connector.created) == 1
        assert connector.created[0].commands == ["show version", "show running-config"]

    def test_disconnects(self, device) -> None:
        connector = make_connector()
        connect.collect(device, ["show version"], connector=connector)
        assert connector.created[0].disconnected

    def test_no_enable_without_a_secret(self, device) -> None:
        connector = make_connector()
        connect.collect(device, ["show version"], connector=connector)
        assert connector.created[0].enabled is False

    def test_enable_when_a_secret_is_set(self, device) -> None:
        with_secret = replace(
            device, credentials=replace(device.credentials, enable_secret="s3cret")
        )
        connector = make_connector()
        connect.collect(with_secret, ["show version"], connector=connector)

        assert connector.created[0].enabled is True
        assert connector.created[0].params["secret"] == "s3cret"

    def test_timeouts_are_passed_through(self, device) -> None:
        tuned = replace(device, conn_timeout=7, read_timeout=45)
        connector = make_connector()
        connect.collect(tuned, ["show version"], connector=connector)

        assert connector.created[0].params["conn_timeout"] == 7
        assert connector.created[0].read_timeouts == [45]


class TestCollectFailure:
    def test_never_raises(self, device) -> None:
        connector = make_connector(fail_times=1, error=RuntimeError("boom"))
        result = connect.collect(device, ["show version"], connector=connector)
        assert result.status is DeviceStatus.ERROR
        assert not result.ok

    def test_auth_failure_is_reported(self, device) -> None:
        connector = make_connector(
            fail_times=1, error=NetmikoAuthenticationException("nope")
        )
        result = connect.collect(device, ["show version"], connector=connector)

        assert result.status is DeviceStatus.AUTH_FAILED
        assert "authentication" in result.error.lower()

    def test_mid_command_failure_is_reported(self, device) -> None:
        connector = make_connector(raise_on_command=ReadTimeout("stalled"))
        result = connect.collect(device, ["show running-config"], connector=connector)

        assert result.status is DeviceStatus.COMMAND_FAILED
        # Teardown still runs so the session is not leaked.
        assert connector.created[0].disconnected

    def test_duration_is_recorded(self, device) -> None:
        connector = make_connector(fail_times=1, error=RuntimeError("boom"))
        result = connect.collect(device, ["show version"], connector=connector)
        assert result.duration_s >= 0.0


class TestRetries:
    def test_transient_failure_is_retried(self, device) -> None:
        connector = make_connector(
            fail_times=2, error=NetmikoTimeoutException("TCP connection to device failed.")
        )
        result = connect.collect(
            device, ["show version"], retries=3, retry_delay=0, connector=connector
        )

        assert result.ok
        assert result.attempts == 3

    def test_retries_are_bounded(self, device) -> None:
        connector = make_connector(
            fail_times=99, error=NetmikoTimeoutException("TCP connection to device failed.")
        )
        result = connect.collect(
            device, ["show version"], retries=2, retry_delay=0, connector=connector
        )

        assert result.status is DeviceStatus.UNREACHABLE
        assert result.attempts == 2

    def test_auth_failure_is_not_retried(self, device) -> None:
        """A wrong password will still be wrong on attempt five."""
        calls = {"n": 0}

        def connector(**params):
            calls["n"] += 1
            raise NetmikoAuthenticationException("nope")

        result = connect.collect(
            device, ["show version"], retries=5, retry_delay=0, connector=connector
        )

        assert calls["n"] == 1
        assert result.attempts == 1

    def test_config_error_is_not_retried(self, device) -> None:
        calls = {"n": 0}

        def connector(**params):
            calls["n"] += 1
            raise ValueError("Unsupported device_type: nonsense")

        connect.collect(
            device, ["show version"], retries=5, retry_delay=0, connector=connector
        )
        assert calls["n"] == 1
