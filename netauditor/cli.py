"""Command-line interface.

Exit codes:
    0  everything succeeded
    1  the run completed but one or more devices failed
    2  the run could not start (bad inventory, missing credentials, bad args)
  130  interrupted
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from typing import Sequence

from . import (
    __version__,
    backup,
    compliance,
    gitstore,
    golden,
    inventory,
    render,
    report,
    runner,
)
from .errors import AuditorError, CredentialError, InventoryError
from .models import BackupStatus

log = logging.getLogger("netauditor")

EXIT_OK = 0
EXIT_DEVICE_FAILURE = 1
EXIT_CONFIG_ERROR = 2
EXIT_INTERRUPTED = 130

DEFAULT_INVENTORY = "inventory.yaml"
DEFAULT_BACKUP_DIR = "backups"
DEFAULT_GOLDEN = "golden.yaml"


def _split_csv(values: Sequence[str] | None) -> list[str]:
    """Accept both ``--device a --device b`` and ``--device a,b``."""
    if not values:
        return []
    out: list[str] = []
    for value in values:
        out.extend(part.strip() for part in value.split(",") if part.strip())
    return out


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="auditor.py",
        description=(
            "Back up network device configs over SSH and audit them against a "
            "golden baseline."
        ),
        epilog=(
            "examples:\n"
            "  python auditor.py --list-devices\n"
            "  python auditor.py --test-connection\n"
            "  python auditor.py --backup\n"
            "  python auditor.py --backup --git-commit\n"
            "  python auditor.py --check --report reports/compliance.html\n"
            "  python auditor.py --test-connection --tag lab --json\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")

    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument(
        "-l",
        "--list-devices",
        action="store_true",
        help="parse the inventory and show what the auditor sees (no SSH)",
    )
    action.add_argument(
        "-t",
        "--test-connection",
        action="store_true",
        help="SSH to each device and run its backup command; report reachability",
    )
    action.add_argument(
        "-b",
        "--backup",
        action="store_true",
        help="collect each device's running config and write it to --backup-dir",
    )
    action.add_argument(
        "-k",
        "--check",
        action="store_true",
        help="audit each device against the golden config; exit 1 on any violation",
    )

    source = parser.add_argument_group("device selection")
    source.add_argument(
        "-i",
        "--inventory",
        default=DEFAULT_INVENTORY,
        metavar="PATH",
        help=f"inventory file (default: {DEFAULT_INVENTORY})",
    )
    source.add_argument(
        "-d",
        "--device",
        action="append",
        metavar="NAME",
        help="limit to these devices (repeatable, or comma-separated)",
    )
    source.add_argument(
        "--tag",
        action="append",
        metavar="TAG",
        help="limit to devices carrying any of these tags",
    )

    conn = parser.add_argument_group("connection")
    conn.add_argument(
        "-c",
        "--command",
        action="append",
        metavar="CMD",
        help=(
            "command to run instead of each device's backup command "
            "(repeatable; --test-connection only)"
        ),
    )
    conn.add_argument(
        "-w",
        "--workers",
        type=int,
        default=runner.DEFAULT_WORKERS,
        help=f"max concurrent SSH sessions (default: {runner.DEFAULT_WORKERS})",
    )
    conn.add_argument(
        "--retries",
        type=int,
        default=1,
        metavar="N",
        help="total attempts per device for transient failures (default: 1)",
    )
    conn.add_argument(
        "--retry-delay",
        type=float,
        default=2.0,
        metavar="SECONDS",
        help="delay between retries (default: 2.0)",
    )

    backup_group = parser.add_argument_group("backup (--backup only)")
    backup_group.add_argument(
        "--backup-dir",
        default=DEFAULT_BACKUP_DIR,
        metavar="PATH",
        help=f"where configs are written (default: {DEFAULT_BACKUP_DIR})",
    )
    backup_group.add_argument(
        "--force",
        action="store_true",
        help="write a new file even if the config is unchanged",
    )
    backup_group.add_argument(
        "--git-commit",
        action="store_true",
        help="git-commit newly written backups, giving configs a change history",
    )

    check_group = parser.add_argument_group("compliance (--check only)")
    check_group.add_argument(
        "-g",
        "--golden",
        default=DEFAULT_GOLDEN,
        metavar="PATH",
        help=f"golden config rules (default: {DEFAULT_GOLDEN})",
    )
    check_group.add_argument(
        "--live",
        action="store_true",
        help=(
            "SSH to the devices and audit their current config, instead of "
            "auditing the most recent backup"
        ),
    )
    check_group.add_argument(
        "--report",
        metavar="PATH",
        help=(
            "also write the report to a file; format comes from the extension "
            "(.html, .md, .json)"
        ),
    )

    out = parser.add_argument_group("output")
    out.add_argument(
        "--json",
        action="store_true",
        help="emit JSON on stdout instead of a table",
    )
    out.add_argument(
        "--show-output",
        action="store_true",
        help="include raw command output in --json (large)",
    )
    out.add_argument("--no-color", action="store_true", help="disable ANSI color")
    out.add_argument(
        "-v",
        "--verbose",
        action="count",
        default=0,
        help="-v for info, -vv for debug (including SSH library logs)",
    )

    env = parser.add_argument_group("environment")
    env.add_argument(
        "--env-file",
        metavar="PATH",
        help="load credentials from this .env file (default: ./.env if present)",
    )

    return parser


def configure_logging(verbosity: int) -> None:
    """Logs go to stderr so stdout stays clean for --json."""
    level = logging.WARNING
    if verbosity == 1:
        level = logging.INFO
    elif verbosity >= 2:
        level = logging.DEBUG

    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stderr,
    )
    # paramiko is extremely chatty at DEBUG; only opt in at -vv.
    if verbosity < 2:
        logging.getLogger("paramiko").setLevel(logging.WARNING)
        logging.getLogger("netmiko").setLevel(logging.WARNING)


def load_env_file(path: str | None) -> None:
    """Load a .env file if python-dotenv is installed.

    ``override=False`` on purpose: real environment variables (CI secrets) must
    win over a stale local .env file.
    """
    try:
        from dotenv import load_dotenv
    except ImportError:
        if path:
            log.warning("python-dotenv is not installed; ignoring --env-file %s", path)
        return

    target = Path(path) if path else Path(".env")
    if path and not target.is_file():
        raise InventoryError(f"env file not found: {target}")
    if target.is_file():
        load_dotenv(target, override=False)
        log.info("loaded environment from %s", target)


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    configure_logging(args.verbose)

    console = report.make_console(no_color=args.no_color)
    err_console = report.make_console(no_color=args.no_color, stderr=True)

    if args.command and not args.test_connection:
        parser.error("--command only applies to --test-connection")
    if args.workers < 1:
        parser.error("--workers must be at least 1")
    if args.retries < 1:
        parser.error("--retries must be at least 1")

    try:
        load_env_file(args.env_file)
        devices = inventory.load_inventory(args.inventory)
        devices = inventory.filter_devices(
            devices,
            names=_split_csv(args.device),
            tags=_split_csv(args.tag),
        )
    except (InventoryError, CredentialError) as exc:
        err_console.print(f"[bold red]error:[/] {exc}")
        return EXIT_CONFIG_ERROR
    except AuditorError as exc:
        err_console.print(f"[bold red]error:[/] {exc}")
        return EXIT_CONFIG_ERROR

    if not devices:
        err_console.print("[bold yellow]warning:[/] no devices matched the given filters")
        return EXIT_CONFIG_ERROR

    if args.list_devices:
        if args.json:
            console.print_json(
                report.dump_json(
                    {
                        "devices": [
                            {
                                "name": d.name,
                                "host": d.host,
                                "port": d.port,
                                "device_type": d.device_type,
                                "tags": list(d.tags),
                                "backup_command": d.backup_command,
                                "username": d.credentials.username,
                                "auth": "key" if d.credentials.uses_key else "password",
                            }
                            for d in devices
                        ]
                    }
                )
            )
        else:
            report.render_device_table(devices, console)
        return EXIT_OK

    # --check reads backups off disk by default, so it needs no network at all.
    if args.check and not args.live:
        try:
            rules = golden.load_golden(args.golden)
        except AuditorError as exc:
            err_console.print(f"[bold red]error:[/] {exc}")
            return EXIT_CONFIG_ERROR
        audited = compliance.audit_from_backups(devices, rules, args.backup_dir)
        return _report_compliance(args, audited, console)

    # Everything else collects from the devices first.
    commands = _split_csv(args.command) or None

    try:
        results = runner.run(
            devices,
            commands,
            workers=args.workers,
            retries=args.retries,
            retry_delay=args.retry_delay,
        )
    except KeyboardInterrupt:
        err_console.print("[bold yellow]interrupted[/]")
        return EXIT_INTERRUPTED

    if args.backup:
        return _run_backup(args, results, console, err_console)

    if args.check:  # --check --live
        try:
            rules = golden.load_golden(args.golden)
        except AuditorError as exc:
            err_console.print(f"[bold red]error:[/] {exc}")
            return EXIT_CONFIG_ERROR
        return _report_compliance(
            args, compliance.audit_from_results(results, rules), console
        )

    if args.json:
        print(
            report.dump_json(
                report.results_to_json(results, include_output=args.show_output)
            )
        )
    else:
        report.render_results_table(results, console)
        report.render_summary(results, console)

    return EXIT_OK if all(r.ok for r in results) else EXIT_DEVICE_FAILURE


#: report file extension -> renderer
REPORT_RENDERERS = {
    ".html": render.to_html,
    ".htm": render.to_html,
    ".md": render.to_markdown,
    ".markdown": render.to_markdown,
}


def _write_report(path_str: str, audited) -> Path:
    """Write the audit to a file, choosing the format from the extension."""
    path = Path(path_str)
    suffix = path.suffix.lower()

    if suffix == ".json":
        text = report.dump_json(report.compliance_to_json(audited))
    else:
        renderer = REPORT_RENDERERS.get(suffix)
        if renderer is None:
            raise InventoryError(
                f"cannot infer report format from {path.name!r}. "
                f"Use one of: {', '.join(sorted({*REPORT_RENDERERS, '.json'}))}"
            )
        text = renderer(audited)

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")
    return path


def _report_compliance(args, audited, console) -> int:
    """Render the audit and pick the exit code.

    Non-zero on any violation is the point: it is what lets a scheduled CI job
    fail the build when a device drifts.
    """
    if args.report:
        try:
            written = _write_report(args.report, audited)
        except (InventoryError, OSError) as exc:
            # The audit itself succeeded; report the write failure without
            # discarding the result.
            console.print(f"[bold red]could not write report:[/] {exc}")
            return EXIT_CONFIG_ERROR
        console.print(f"[bold]Report:[/] {written}")

    if args.json:
        print(report.dump_json(report.compliance_to_json(audited)))
    else:
        report.render_compliance_table(audited, console)
        report.render_violations(audited, console)
        report.render_compliance_summary(audited, console)

    return EXIT_OK if all(r.compliant for r in audited) else EXIT_DEVICE_FAILURE


def _run_backup(args, results, console, err_console) -> int:
    """Write collected configs to disk, and optionally commit them."""
    backups = backup.backup_all(results, args.backup_dir, force=args.force)

    commit: str | None = None
    commit_error: str | None = None
    if args.git_commit:
        written = backup.written_paths(backups)
        if written:
            names = [
                b.device.name for b in backups if b.status is BackupStatus.WRITTEN
            ]
            try:
                commit = gitstore.commit_paths(
                    written,
                    Path.cwd(),
                    gitstore.default_message(len(written), names),
                )
            except (gitstore.GitError, OSError) as exc:
                # The configs are safely on disk; a failed commit must not
                # turn a successful backup into a failed run.
                commit_error = str(exc)
                log.warning("git commit failed: %s", exc)

    if args.json:
        payload = report.backups_to_json(backups)
        payload["git"] = {"commit": commit, "error": commit_error}
        print(report.dump_json(payload))
    else:
        report.render_backup_table(backups, console)
        report.render_backup_summary(backups, console)
        if commit:
            console.print(f"[bold]Committed:[/] {commit}")
        elif commit_error:
            err_console.print(f"[bold yellow]git commit failed:[/] {commit_error}")

    return EXIT_OK if all(b.ok for b in backups) else EXIT_DEVICE_FAILURE


def run_cli() -> None:
    """Console entry point: translate the return code into an exit."""
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(EXIT_INTERRUPTED)
