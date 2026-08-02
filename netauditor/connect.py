"""Netmiko transport layer.

Everything network-facing lives here, and nothing here raises: a failure on one
device becomes a :class:`DeviceResult` with a non-success status so a single
dead box cannot abort the run.
"""

from __future__ import annotations

import logging
import socket
import time
from typing import Any, Callable, Sequence

from netmiko import ConnectHandler
from netmiko.exceptions import (
    NetmikoAuthenticationException,
    NetmikoTimeoutException,
    ReadTimeout,
)
from paramiko.ssh_exception import AuthenticationException, SSHException

from .models import RETRYABLE_STATUSES, Device, DeviceResult, DeviceStatus

log = logging.getLogger(__name__)

#: Injectable so tests can substitute a fake connection without a lab.
Connector = Callable[..., Any]

#: Substrings that mark a NetmikoTimeoutException as "never got a TCP session"
#: rather than "connected, then the read stalled".
_UNREACHABLE_MARKERS = (
    "tcp connection to device failed",
    "connection to device failed",
    "name or service not known",
    "no route to host",
    "network is unreachable",
    "connection refused",
)


def classify_exception(exc: BaseException) -> tuple[DeviceStatus, str]:
    """Map an exception to a status and a one-line, human-readable reason.

    Kept as a pure function so the error taxonomy is unit-testable without
    standing up a device.
    """
    message = str(exc).strip().splitlines()
    first_line = message[0] if message else exc.__class__.__name__

    if isinstance(exc, (NetmikoAuthenticationException, AuthenticationException)):
        return DeviceStatus.AUTH_FAILED, "authentication failed (check username/password)"

    if isinstance(exc, NetmikoTimeoutException):
        lowered = str(exc).lower()
        if any(marker in lowered for marker in _UNREACHABLE_MARKERS):
            return DeviceStatus.UNREACHABLE, "could not open TCP session to device"
        return DeviceStatus.TIMEOUT, "timed out waiting for the device"

    if isinstance(exc, ReadTimeout):
        return DeviceStatus.COMMAND_FAILED, "device stopped responding mid-command"

    if isinstance(exc, socket.gaierror):
        return DeviceStatus.UNREACHABLE, "hostname did not resolve"

    if isinstance(exc, (socket.timeout, TimeoutError)):
        return DeviceStatus.TIMEOUT, "socket timed out"

    if isinstance(exc, (ConnectionRefusedError, OSError)) and not isinstance(exc, SSHException):
        return DeviceStatus.UNREACHABLE, f"network error: {first_line}"

    if isinstance(exc, SSHException):
        return DeviceStatus.ERROR, f"SSH error: {first_line}"

    if isinstance(exc, ValueError):
        # ConnectHandler raises ValueError for an unsupported device_type.
        return DeviceStatus.CONFIG_ERROR, first_line

    return DeviceStatus.ERROR, f"{exc.__class__.__name__}: {first_line}"


def _run_once(
    device: Device,
    commands: Sequence[str],
    connector: Connector,
) -> dict[str, str]:
    """Open one session, run every command, close. Exceptions propagate."""
    params = device.netmiko_params()
    log.debug("connecting to %s as %s", device.label, device.credentials.username)

    outputs: dict[str, str] = {}
    connection = connector(**params)
    try:
        if device.credentials.enable_secret:
            connection.enable()
        for command in commands:
            log.debug("%s: running %r", device.name, command)
            outputs[command] = connection.send_command(
                command, read_timeout=device.read_timeout
            )
    finally:
        try:
            connection.disconnect()
        except Exception:  # noqa: BLE001 - a failed teardown must not mask the real error
            log.debug("%s: error during disconnect", device.name, exc_info=True)
    return outputs


def collect(
    device: Device,
    commands: Sequence[str],
    *,
    retries: int = 1,
    retry_delay: float = 2.0,
    connector: Connector = ConnectHandler,
) -> DeviceResult:
    """Run ``commands`` on ``device`` and return a result. Never raises.

    Args:
        retries: total attempts, including the first. Only transient failures
            (unreachable / timeout) are retried; an auth failure or a bad
            device_type will not fix itself.
        connector: factory used to build the session. Defaults to
            ``netmiko.ConnectHandler``; tests pass a fake.
    """
    attempts = max(1, retries)
    started = time.monotonic()
    status = DeviceStatus.ERROR
    reason: str | None = None

    for attempt in range(1, attempts + 1):
        try:
            outputs = _run_once(device, commands, connector)
        except Exception as exc:  # noqa: BLE001 - deliberate: classify, do not crash
            status, reason = classify_exception(exc)
            log.debug(
                "%s: attempt %d/%d failed (%s)",
                device.name,
                attempt,
                attempts,
                status.value,
                exc_info=True,
            )
            if status in RETRYABLE_STATUSES and attempt < attempts:
                time.sleep(retry_delay)
                continue
            return DeviceResult(
                device=device,
                status=status,
                error=reason,
                duration_s=time.monotonic() - started,
                attempts=attempt,
            )
        else:
            return DeviceResult(
                device=device,
                status=DeviceStatus.SUCCESS,
                outputs=outputs,
                duration_s=time.monotonic() - started,
                attempts=attempt,
            )

    # Unreachable in practice; keeps the return type honest.
    return DeviceResult(
        device=device,
        status=status,
        error=reason,
        duration_s=time.monotonic() - started,
        attempts=attempts,
    )
