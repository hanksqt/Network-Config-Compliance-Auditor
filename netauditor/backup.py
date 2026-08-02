"""Write collected configs to ``backups/<device>/<timestamp>.cfg``.

Three things this module is careful about, because the backup directory is the
tool's memory and a bad write is worse than no write:

1. **Sanity-check before writing.** A device that answers with an error string
   must not overwrite a directory of good configs. See
   :func:`~netauditor.connect.output_problem` for the transport-level half of
   this; here we check the config actually looks like a config.
2. **Skip identical captures.** Running hourly should not produce 24 identical
   files a day. Only real changes get a new file, so the directory listing is
   a change history rather than a cron log.
3. **Write atomically.** A crash mid-write leaves the previous backup intact
   rather than a truncated file that looks like a config.
"""

from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Sequence

from .models import BackupResult, BackupStatus, Device, DeviceResult

log = logging.getLogger(__name__)

BACKUP_SUFFIX = ".cfg"

#: Filesystem-safe, sorts chronologically, unambiguous about timezone.
TIMESTAMP_FORMAT = "%Y%m%dT%H%M%SZ"

#: A config shorter than this is not plausibly a device configuration. Catches
#: truncated reads and error replies that slipped past output_problem().
MIN_CONFIG_LINES = 5

#: Lines that change on every capture without the config having changed.
#: Ignored when comparing against the previous backup, otherwise every run
#: would look like drift. They are still written to the file verbatim.
VOLATILE_LINE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^\s*!\s*Command:", re.I),
    re.compile(r"^\s*!\s*Last configuration change", re.I),
    re.compile(r"^\s*!\s*NVRAM config last updated", re.I),
    re.compile(r"^\s*!\s*Startup-config last modified", re.I),
    re.compile(r"^\s*!\s*Time:", re.I),
    re.compile(r"^\s*!\s*Running configuration last done", re.I),
    re.compile(r"^\s*Current configuration\s*:\s*\d+ bytes", re.I),
    re.compile(r"^\s*Building configuration", re.I),
    re.compile(r"^\s*ntp clock-period", re.I),
)


def is_volatile(line: str) -> bool:
    return any(pattern.match(line) for pattern in VOLATILE_LINE_PATTERNS)


def normalize(config: str) -> str:
    """Reduce a config to what should be compared between captures.

    Drops volatile header lines, trailing whitespace, and leading/trailing
    blank lines. Used only for change detection — the file on disk keeps the
    device's exact output.
    """
    kept = [
        line.rstrip()
        for line in config.replace("\r\n", "\n").replace("\r", "\n").splitlines()
        if not is_volatile(line)
    ]
    while kept and not kept[0]:
        kept.pop(0)
    while kept and not kept[-1]:
        kept.pop()
    return "\n".join(kept)


def config_problem(config: str) -> str | None:
    """Return a reason the config should not be written, or ``None``."""
    meaningful = [line for line in normalize(config).splitlines() if line.strip()]
    if not meaningful:
        return "config is empty"
    if len(meaningful) < MIN_CONFIG_LINES:
        return (
            f"config has only {len(meaningful)} meaningful line(s), "
            f"expected at least {MIN_CONFIG_LINES}"
        )
    return None


def device_dir(backup_root: Path | str, device: Device) -> Path:
    return Path(backup_root) / device.name


def existing_backups(directory: Path) -> list[Path]:
    """Backups for one device, oldest first (timestamp format sorts naturally)."""
    if not directory.is_dir():
        return []
    return sorted(p for p in directory.glob(f"*{BACKUP_SUFFIX}") if p.is_file())


def latest_backup(directory: Path) -> Path | None:
    backups = existing_backups(directory)
    return backups[-1] if backups else None


def _write_atomic(path: Path, text: str) -> None:
    """Write via a temp file in the same directory, then rename.

    os.replace is atomic on the same filesystem, so a crash cannot leave a
    half-written file where a config should be.
    """
    tmp = path.with_name(path.name + ".tmp")
    try:
        with tmp.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def write_backup(
    device: Device,
    config: str,
    backup_root: Path | str,
    *,
    timestamp: datetime | None = None,
    force: bool = False,
) -> BackupResult:
    """Write one device's config. Never raises.

    Args:
        force: write even if the config is unchanged from the last capture.
    """
    directory = device_dir(backup_root, device)

    problem = config_problem(config)
    if problem:
        log.warning("%s: refusing to write backup - %s", device.name, problem)
        return BackupResult(
            device=device, status=BackupStatus.REJECTED, error=problem
        )

    # Normalize line endings on the way in; .gitattributes keeps .cfg at LF so
    # a Windows checkout does not show every backup as modified.
    text = config.replace("\r\n", "\n").replace("\r", "\n")
    if not text.endswith("\n"):
        text += "\n"
    line_count = len(text.splitlines())

    previous = latest_backup(directory)
    if previous is not None and not force:
        try:
            if normalize(previous.read_text(encoding="utf-8")) == normalize(text):
                log.debug("%s: unchanged since %s", device.name, previous.name)
                return BackupResult(
                    device=device,
                    status=BackupStatus.UNCHANGED,
                    path=previous,
                    lines=line_count,
                )
        except OSError as exc:
            # An unreadable previous backup should not block a new one.
            log.debug("%s: could not read %s (%s)", device.name, previous, exc)

    stamp = (timestamp or datetime.now(timezone.utc)).strftime(TIMESTAMP_FORMAT)
    path = directory / f"{stamp}{BACKUP_SUFFIX}"

    try:
        directory.mkdir(parents=True, exist_ok=True)
        _write_atomic(path, text)
    except OSError as exc:
        log.error("%s: could not write %s (%s)", device.name, path, exc)
        return BackupResult(
            device=device, status=BackupStatus.FAILED, path=path, error=str(exc)
        )

    log.info("%s: wrote %s (%d lines)", device.name, path, line_count)
    return BackupResult(
        device=device, status=BackupStatus.WRITTEN, path=path, lines=line_count
    )


def backup_all(
    results: Sequence[DeviceResult],
    backup_root: Path | str,
    *,
    timestamp: datetime | None = None,
    force: bool = False,
) -> list[BackupResult]:
    """Write a backup for every device that was collected successfully.

    Devices whose collection failed are reported as SKIPPED rather than
    dropped, so the summary still accounts for every device in the inventory.
    """
    backups: list[BackupResult] = []

    for result in results:
        if not result.ok:
            backups.append(
                BackupResult(
                    device=result.device,
                    status=BackupStatus.SKIPPED,
                    error=result.error or "collection failed",
                )
            )
            continue

        config = result.outputs.get(result.device.backup_command)
        if config is None:
            # Only reachable if the caller ran different commands than the
            # device's backup_command.
            backups.append(
                BackupResult(
                    device=result.device,
                    status=BackupStatus.SKIPPED,
                    error=f"no output for {result.device.backup_command!r}",
                )
            )
            continue

        backups.append(
            write_backup(
                result.device,
                config,
                backup_root,
                timestamp=timestamp,
                force=force,
            )
        )

    return backups


def written_paths(backups: Iterable[BackupResult]) -> list[Path]:
    return [b.path for b in backups if b.status is BackupStatus.WRITTEN and b.path]
