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

from . import __version__, inventory, report, runner
from .errors import AuditorError, CredentialError, InventoryError

log = logging.getLogger("netauditor")

EXIT_OK = 0
EXIT_DEVICE_FAILURE = 1
EXIT_CONFIG_ERROR = 2
EXIT_INTERRUPTED = 130

DEFAULT_INVENTORY = "inventory.yaml"


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

    # --test-connection
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


def run_cli() -> None:
    """Console entry point: translate the return code into an exit."""
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(EXIT_INTERRUPTED)
