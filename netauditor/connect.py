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


#: Markers that mean the device rejected the command instead of answering it.
DEVICE_ERROR_MARKERS = (
    "invalid input",
    "incomplete command",
    "ambiguous command",
    "permission denied",
    "authorization failed",
    "invalid command",
    "unknown command",
    "syntax error",
)

#: A rejection is short; a real config is not. Only treat a marker as an error
#: when the whole reply is small enough to plausibly *be* the error, so a
#: config with "syntax error" in a banner is not misread as a failure.
MAX_ERROR_REPLY_LINES = 5


def output_problem(command: str, output: str) -> str | None:
    """Return a reason if the device rejected ``command``, else ``None``.

    Netmiko only reports transport failures. A device that answers "% Invalid
    input" has replied perfectly well at the SSH layer, so without this check
    an error string gets stored as if it were a configuration.
    """
    text = output.strip()
    if not text:
        return f"{command!r} returned no output"

    lines = text.splitlines()
    if len(lines) <= MAX_ERROR_REPLY_LINES:
        first = lines[0].strip()
        lowered = first.lower()
        if first.startswith("%") or any(m in lowered for m in DEVICE_ERROR_MARKERS):
            return f"device rejected {command!r}: {first}"
    return None


def _enter_enable_mode(connection: Any, device: Device) -> None:
    """Get into privileged mode before running show commands.

    Netmiko only auto-enables when a secret is set, but on EOS and IOS a
    privilege-15 account typically enters enable with no password at all --
    and without it `show running-config` is rejected with
    "% Invalid input (privileged mode required)".

    Platforms with no enable mode (linux, junos) raise here; that is not an
    error, so it is swallowed unless the inventory explicitly configured a
    secret, in which case failing to use it is worth surfacing.
    """
    try:
        if not connection.check_enable_mode():
            connection.enable()
    except Exception:
        if device.credentials.enable_secret:
            raise
        log.debug(
            "%s: no enable mode available, continuing in user exec",
            device.name,
            exc_info=True,
        )


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
        _enter_enable_mode(connection, device)
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
            # The SSH layer succeeded; the device may still have rejected the
            # command. Not retried -- a rejected command stays rejected.
            problem = next(
                (p for c, o in outputs.items() if (p := output_problem(c, o))),
                None,
            )
            return DeviceResult(
                device=device,
                status=(
                    DeviceStatus.COMMAND_FAILED if problem else DeviceStatus.SUCCESS
                ),
                outputs=outputs,
                error=problem,
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
