"""Backup writing: sanity checks, change detection, atomicity.

The backup directory is the tool's memory. A bad write is worse than no write,
so most of these tests are about refusing to write.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from netauditor import backup
from netauditor.models import BackupStatus, DeviceResult, DeviceStatus

REAL_CONFIG = """\
! Command: show running-config
! device: ceos-spine1 (cEOSLab, EOS-4.33.9M)
!
no aaa root
!
username admin privilege 15 role network-admin secret sha512 $6$abc
!
hostname ceos-spine1
!
ip routing
!
end
"""

TS = datetime(2026, 8, 1, 23, 15, 30, tzinfo=timezone.utc)


class TestNormalize:
    def test_drops_volatile_header_lines(self) -> None:
        """Otherwise every capture looks like a change."""
        a = "! Command: show running-config\nhostname r1\nip routing"
        b = "! Command: show running-config all\nhostname r1\nip routing"
        assert backup.normalize(a) == backup.normalize(b)

    @pytest.mark.parametrize(
        "line",
        [
            "! Last configuration change at 12:00:00 UTC Mon Aug 1 2026",
            "! NVRAM config last updated at 09:14:22 UTC",
            "Current configuration : 4021 bytes",
            "Building configuration...",
            "ntp clock-period 17179862",
            "! Time: Sat Aug  1 23:15:30 2026",
        ],
    )
    def test_known_volatile_lines(self, line: str) -> None:
        assert backup.is_volatile(line)

    def test_real_config_lines_are_not_volatile(self) -> None:
        for line in ("hostname r1", "ip routing", "!", "username admin privilege 15"):
            assert not backup.is_volatile(line)

    def test_normalizes_line_endings_and_trailing_space(self) -> None:
        assert backup.normalize("hostname r1  \r\nip routing\r\n") == (
            "hostname r1\nip routing"
        )


class TestConfigProblem:
    def test_real_config_accepted(self) -> None:
        assert backup.config_problem(REAL_CONFIG) is None

    def test_empty_rejected(self) -> None:
        assert backup.config_problem("   \n\n  ") is not None

    def test_error_string_rejected(self) -> None:
        """The exact failure the lab produced, as a last line of defence."""
        problem = backup.config_problem("% Invalid input (privileged mode required)")
        assert problem is not None

    def test_truncated_config_rejected(self) -> None:
        assert backup.config_problem("hostname r1\nip routing") is not None


class TestWriteBackup:
    def test_writes_timestamped_file(self, device, tmp_path: Path) -> None:
        result = backup.write_backup(device, REAL_CONFIG, tmp_path, timestamp=TS)

        assert result.status is BackupStatus.WRITTEN
        assert result.path == tmp_path / "ceos-spine1" / "20260801T231530Z.cfg"
        assert result.path.read_text(encoding="utf-8") == REAL_CONFIG

    def test_creates_per_device_directory(self, device, tmp_path: Path) -> None:
        backup.write_backup(device, REAL_CONFIG, tmp_path, timestamp=TS)
        assert (tmp_path / "ceos-spine1").is_dir()

    def test_rejects_error_string_without_writing(self, device, tmp_path: Path) -> None:
        """A device answering with an error must not overwrite good history."""
        result = backup.write_backup(
            device, "% Invalid input (privileged mode required)", tmp_path
        )

        assert result.status is BackupStatus.REJECTED
        assert not (tmp_path / "ceos-spine1").exists()

    def test_writes_lf_endings(self, device, tmp_path: Path) -> None:
        result = backup.write_backup(
            device, REAL_CONFIG.replace("\n", "\r\n"), tmp_path, timestamp=TS
        )
        raw = result.path.read_bytes()
        assert b"\r\n" not in raw

    def test_leaves_no_temp_file(self, device, tmp_path: Path) -> None:
        backup.write_backup(device, REAL_CONFIG, tmp_path, timestamp=TS)
        assert list((tmp_path / "ceos-spine1").glob("*.tmp")) == []

    def test_write_failure_is_reported_not_raised(self, device, tmp_path: Path) -> None:
        # A file where the device directory needs to be.
        (tmp_path / "ceos-spine1").write_text("in the way", encoding="utf-8")

        result = backup.write_backup(device, REAL_CONFIG, tmp_path, timestamp=TS)
        assert result.status is BackupStatus.FAILED
        assert result.error


class TestChangeDetection:
    def test_identical_config_is_not_rewritten(self, device, tmp_path: Path) -> None:
        """Hourly runs should not produce 24 identical files a day."""
        first = backup.write_backup(device, REAL_CONFIG, tmp_path, timestamp=TS)
        second = backup.write_backup(
            device, REAL_CONFIG, tmp_path, timestamp=TS.replace(hour=23, minute=45)
        )

        assert second.status is BackupStatus.UNCHANGED
        assert second.path == first.path
        assert len(backup.existing_backups(tmp_path / "ceos-spine1")) == 1

    def test_volatile_only_change_is_not_a_new_backup(self, device, tmp_path: Path) -> None:
        backup.write_backup(device, REAL_CONFIG, tmp_path, timestamp=TS)
        churned = REAL_CONFIG.replace("! Command: show running-config", "! Time: later")

        result = backup.write_backup(
            device, churned, tmp_path, timestamp=TS.replace(minute=45)
        )
        assert result.status is BackupStatus.UNCHANGED

    def test_real_change_writes_a_new_file(self, device, tmp_path: Path) -> None:
        backup.write_backup(device, REAL_CONFIG, tmp_path, timestamp=TS)
        changed = REAL_CONFIG.replace("hostname ceos-spine1", "hostname ceos-spine9")

        result = backup.write_backup(
            device, changed, tmp_path, timestamp=TS.replace(minute=45)
        )
        assert result.status is BackupStatus.WRITTEN
        assert len(backup.existing_backups(tmp_path / "ceos-spine1")) == 2

    def test_force_writes_anyway(self, device, tmp_path: Path) -> None:
        backup.write_backup(device, REAL_CONFIG, tmp_path, timestamp=TS)
        result = backup.write_backup(
            device, REAL_CONFIG, tmp_path, timestamp=TS.replace(minute=45), force=True
        )
        assert result.status is BackupStatus.WRITTEN

    def test_backups_sort_chronologically(self, device, tmp_path: Path) -> None:
        """The filename format has to sort correctly for `latest` to be latest."""
        for minute, host in ((10, "a"), (45, "b"), (20, "c")):
            backup.write_backup(
                device,
                REAL_CONFIG.replace("hostname ceos-spine1", f"hostname {host}"),
                tmp_path,
                timestamp=TS.replace(minute=minute),
            )
        latest = backup.latest_backup(tmp_path / "ceos-spine1")
        assert "hostname b" in latest.read_text(encoding="utf-8")


class TestBackupAll:
    def _result(self, device, status=DeviceStatus.SUCCESS, config=REAL_CONFIG):
        return DeviceResult(
            device=device,
            status=status,
            outputs={device.backup_command: config} if config else {},
            error=None if status.is_ok else "unreachable",
        )

    def test_writes_for_successful_devices(self, device, tmp_path: Path) -> None:
        backups = backup.backup_all([self._result(device)], tmp_path, timestamp=TS)
        assert backups[0].status is BackupStatus.WRITTEN

    def test_failed_collection_is_skipped_not_dropped(self, device, tmp_path: Path) -> None:
        """Every inventory device must appear in the summary."""
        backups = backup.backup_all(
            [self._result(device, DeviceStatus.UNREACHABLE, config=None)],
            tmp_path,
        )
        assert len(backups) == 1
        assert backups[0].status is BackupStatus.SKIPPED
        assert not (tmp_path / "ceos-spine1").exists()

    def test_missing_command_output_is_skipped(self, device, tmp_path: Path) -> None:
        result = DeviceResult(
            device=device, status=DeviceStatus.SUCCESS, outputs={"show version": "x"}
        )
        backups = backup.backup_all([result], tmp_path)
        assert backups[0].status is BackupStatus.SKIPPED

    def test_written_paths_lists_only_new_files(self, device, tmp_path: Path) -> None:
        backup.backup_all([self._result(device)], tmp_path, timestamp=TS)
        second = backup.backup_all(
            [self._result(device)], tmp_path, timestamp=TS.replace(minute=45)
        )
        assert backup.written_paths(second) == []
