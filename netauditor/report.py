"""Console and JSON output.

Console rendering goes to stdout via rich; JSON output is emitted as the *only*
thing on stdout so the tool stays pipeable (`... --json | jq`). All logging goes
to stderr for the same reason.
"""

from __future__ import annotations

import json
import sys
from typing import Any, Sequence

from rich.console import Console
from rich.table import Table
from rich.text import Text

from .models import Device, DeviceResult, DeviceStatus

#: status -> (label, rich style)
STATUS_STYLE: dict[DeviceStatus, tuple[str, str]] = {
    DeviceStatus.SUCCESS: ("OK", "bold green"),
    DeviceStatus.UNREACHABLE: ("UNREACHABLE", "bold red"),
    DeviceStatus.AUTH_FAILED: ("AUTH FAILED", "bold red"),
    DeviceStatus.TIMEOUT: ("TIMEOUT", "bold yellow"),
    DeviceStatus.COMMAND_FAILED: ("CMD FAILED", "bold yellow"),
    DeviceStatus.CONFIG_ERROR: ("CONFIG ERROR", "bold magenta"),
    DeviceStatus.ERROR: ("ERROR", "bold red"),
}


#: Width to assume when output is redirected. rich falls back to 80 columns off
#: a terminal, which squeezes the tables into unreadable ellipses in CI logs.
PIPED_WIDTH = 160


def make_console(*, no_color: bool = False, stderr: bool = False) -> Console:
    stream = sys.stderr if stderr else sys.stdout
    width = None if stream.isatty() else PIPED_WIDTH
    return Console(
        no_color=no_color,
        stderr=stderr,
        highlight=False,
        soft_wrap=False,
        width=width,
    )


def status_text(status: DeviceStatus) -> Text:
    label, style = STATUS_STYLE.get(status, (status.value.upper(), "bold red"))
    return Text(label, style=style)


def render_device_table(devices: Sequence[Device], console: Console) -> None:
    """Table for ``--list-devices``: what the auditor parsed out of the YAML."""
    table = Table(title=f"Inventory ({len(devices)} device(s))", title_justify="left")
    table.add_column("Device", style="bold")
    table.add_column("Host")
    table.add_column("Port", justify="right")
    table.add_column("Platform")
    table.add_column("Tags")
    table.add_column("Auth")
    table.add_column("Backup command")

    for device in devices:
        auth = "ssh key" if device.credentials.uses_key else "password"
        if device.credentials.enable_secret:
            auth += " + enable"
        table.add_row(
            device.name,
            device.host,
            str(device.port),
            device.device_type,
            ", ".join(device.tags) or "-",
            f"{device.credentials.username} ({auth})",
            device.backup_command,
        )

    console.print(table)


def render_results_table(
    results: Sequence[DeviceResult],
    console: Console,
    *,
    title: str = "Connectivity check",
) -> None:
    table = Table(title=title, title_justify="left")
    table.add_column("Device", style="bold")
    table.add_column("Host")
    table.add_column("Platform")
    table.add_column("Status")
    table.add_column("Time", justify="right")
    table.add_column("Detail")

    for result in results:
        detail = result.error or ""
        if result.ok:
            lines = sum(len(out.splitlines()) for out in result.outputs.values())
            detail = f"{lines} line(s) collected"
        if result.attempts > 1:
            detail = f"{detail} (after {result.attempts} attempts)"

        table.add_row(
            result.device.name,
            f"{result.device.host}:{result.device.port}",
            result.device.device_type,
            status_text(result.status),
            f"{result.duration_s:.1f}s",
            detail,
        )

    console.print(table)


def render_summary(results: Sequence[DeviceResult], console: Console) -> None:
    total = len(results)
    ok = sum(1 for r in results if r.ok)
    failed = total - ok
    style = "bold green" if failed == 0 else "bold red"
    console.print(
        Text.assemble(
            ("Summary: ", "bold"),
            (f"{ok}/{total} reachable", style),
            ("" if failed == 0 else f", {failed} failed", "bold red"),
        )
    )


def results_to_json(
    results: Sequence[DeviceResult],
    *,
    include_output: bool = False,
) -> dict[str, Any]:
    """Machine-readable run summary."""
    total = len(results)
    ok = sum(1 for r in results if r.ok)
    return {
        "summary": {
            "total": total,
            "ok": ok,
            "failed": total - ok,
        },
        "devices": [r.to_dict(include_output=include_output) for r in results],
    }


def dump_json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, indent=2, sort_keys=False)
