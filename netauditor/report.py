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

from .models import (
    BackupResult,
    BackupStatus,
    ComplianceResult,
    Device,
    DeviceResult,
    DeviceStatus,
    Severity,
)

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


SEVERITY_STYLE: dict[Severity, str] = {
    Severity.HIGH: "bold red",
    Severity.MEDIUM: "bold yellow",
    Severity.LOW: "cyan",
}


def render_compliance_table(
    results: Sequence[ComplianceResult], console: Console
) -> None:
    """One row per device: the at-a-glance answer."""
    table = Table(title="Compliance check", title_justify="left")
    table.add_column("Device", style="bold")
    table.add_column("Result")
    table.add_column("Rules", justify="right")
    table.add_column("Violations", justify="right")
    table.add_column("Detail")

    for result in results:
        if result.error:
            status = Text("ERROR", style="bold magenta")
            detail = result.error
        elif result.compliant:
            status = Text("COMPLIANT", style="bold green")
            detail = ""
        else:
            status = Text("NON-COMPLIANT", style="bold red")
            detail = ", ".join(r.rule_name for r in result.failures)

        table.add_row(
            result.device.name,
            status,
            f"{len(result.evaluated) - len(result.failures)}/{len(result.evaluated)}",
            str(result.violation_count) or "-",
            detail,
        )

    console.print(table)


def render_violations(results: Sequence[ComplianceResult], console: Console) -> None:
    """The actionable part: exactly which lines are wrong, per device."""
    offenders = [r for r in results if r.failures]
    if not offenders:
        return

    console.print()
    for result in offenders:
        console.print(Text(result.device.name, style="bold underline"))
        for rule in result.failures:
            style = SEVERITY_STYLE.get(rule.severity, "bold red")
            console.print(
                Text.assemble(
                    ("  ✗ ", style),
                    (rule.rule_name, "bold"),
                    (f"  [{rule.severity.value}]", style),
                )
            )
            if rule.description:
                console.print(Text(f"      {rule.description.strip()}", style="dim"))
            for violation in rule.violations:
                console.print(f"      - {violation.describe()}")
        console.print()


def render_compliance_summary(
    results: Sequence[ComplianceResult], console: Console
) -> None:
    total = len(results)
    compliant = sum(1 for r in results if r.compliant)
    errored = sum(1 for r in results if r.error)
    failed = total - compliant - errored

    parts = [f"{compliant}/{total} compliant"]
    if failed:
        parts.append(f"{failed} with violations")
    if errored:
        parts.append(f"{errored} could not be checked")

    console.print(
        Text.assemble(
            ("Compliance: ", "bold"),
            (", ".join(parts), "bold green" if compliant == total else "bold red"),
        )
    )


def compliance_to_json(results: Sequence[ComplianceResult]) -> dict[str, Any]:
    total = len(results)
    compliant = sum(1 for r in results if r.compliant)
    return {
        "summary": {
            "total": total,
            "compliant": compliant,
            "non_compliant": total - compliant,
            "errors": sum(1 for r in results if r.error),
            "violations": sum(r.violation_count for r in results),
        },
        "devices": [r.to_dict() for r in results],
    }


#: backup status -> (label, rich style)
BACKUP_STYLE: dict[BackupStatus, tuple[str, str]] = {
    BackupStatus.WRITTEN: ("WRITTEN", "bold green"),
    BackupStatus.UNCHANGED: ("UNCHANGED", "dim"),
    BackupStatus.SKIPPED: ("SKIPPED", "bold yellow"),
    BackupStatus.REJECTED: ("REJECTED", "bold magenta"),
    BackupStatus.FAILED: ("FAILED", "bold red"),
}


def render_backup_table(backups: Sequence[BackupResult], console: Console) -> None:
    table = Table(title="Config backup", title_justify="left")
    table.add_column("Device", style="bold")
    table.add_column("Status")
    table.add_column("Lines", justify="right")
    table.add_column("File / reason")

    for backup in backups:
        label, style = BACKUP_STYLE.get(
            backup.status, (backup.status.value.upper(), "bold red")
        )
        if backup.status is BackupStatus.WRITTEN and backup.path:
            detail = str(backup.path)
        elif backup.status is BackupStatus.UNCHANGED:
            detail = f"no change since {backup.path.name}" if backup.path else "no change"
        else:
            detail = backup.error or ""

        table.add_row(
            backup.device.name,
            Text(label, style=style),
            str(backup.lines) if backup.lines else "-",
            detail,
        )

    console.print(table)


def render_backup_summary(backups: Sequence[BackupResult], console: Console) -> None:
    counts: dict[BackupStatus, int] = {}
    for backup in backups:
        counts[backup.status] = counts.get(backup.status, 0) + 1

    failed = sum(
        n for status, n in counts.items() if not status.is_ok
    )
    parts = [
        f"{counts.get(BackupStatus.WRITTEN, 0)} written",
        f"{counts.get(BackupStatus.UNCHANGED, 0)} unchanged",
    ]
    if failed:
        parts.append(f"{failed} failed")

    console.print(
        Text.assemble(
            ("Backups: ", "bold"),
            (", ".join(parts), "bold red" if failed else "bold green"),
        )
    )


def backups_to_json(backups: Sequence[BackupResult]) -> dict[str, Any]:
    counts: dict[str, int] = {}
    for backup in backups:
        counts[backup.status.value] = counts.get(backup.status.value, 0) + 1
    return {
        "summary": {
            "total": len(backups),
            "ok": sum(1 for b in backups if b.ok),
            "failed": sum(1 for b in backups if not b.ok),
            "by_status": counts,
        },
        "backups": [b.to_dict() for b in backups],
    }


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
