"""CLI wiring: argument handling and exit codes.

Exit codes matter beyond ergonomics — the GitHub Actions workflow in Phase 5
fails the build on a non-zero exit, so these are part of the contract.
"""

from __future__ import annotations

import json

import pytest
import yaml

from netauditor import cli
from netauditor.models import DeviceResult, DeviceStatus

INVENTORY = {
    "defaults": {"device_type": "arista_eos", "credentials": "lab"},
    "devices": [
        {"name": "spine1", "host": "172.20.20.11", "tags": ["lab", "spine"]},
        {"name": "leaf1", "host": "172.20.20.12", "tags": ["lab", "leaf"]},
    ],
}


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch, lab_env):
    """Known credentials, and never read a developer's real .env."""
    for key, value in lab_env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(cli, "load_env_file", lambda path: None)


@pytest.fixture
def inventory_path(tmp_path):
    path = tmp_path / "inventory.yaml"
    path.write_text(yaml.safe_dump(INVENTORY, sort_keys=False), encoding="utf-8")
    return path


@pytest.fixture
def fake_run(monkeypatch):
    """Replace the network layer; returns a setter for the statuses to report."""
    state: dict[str, DeviceStatus] = {}

    def _run(devices, commands=None, **kwargs):
        return [
            DeviceResult(
                device=d,
                status=state.get(d.name, DeviceStatus.SUCCESS),
                outputs={"show running-config": "hostname " + d.name},
                error=None if state.get(d.name, DeviceStatus.SUCCESS).is_ok else "boom",
            )
            for d in devices
        ]

    monkeypatch.setattr(cli.runner, "run", _run)
    return state


class TestListDevices:
    def test_exits_clean(self, inventory_path, capsys) -> None:
        assert cli.main(["--list-devices", "-i", str(inventory_path)]) == cli.EXIT_OK
        assert "spine1" in capsys.readouterr().out

    def test_json_is_parseable(self, inventory_path, capsys) -> None:
        cli.main(["--list-devices", "-i", str(inventory_path), "--json", "--no-color"])
        payload = json.loads(capsys.readouterr().out)
        assert [d["name"] for d in payload["devices"]] == ["spine1", "leaf1"]

    def test_output_never_contains_a_password(
        self, inventory_path, monkeypatch, capsys
    ) -> None:
        monkeypatch.setenv("NETAUDIT_LAB_PASSWORD", "sup3r-s3cret-pw")
        cli.main(["--list-devices", "-i", str(inventory_path), "--json", "--no-color"])

        captured = capsys.readouterr()
        assert "sup3r-s3cret-pw" not in captured.out
        assert "sup3r-s3cret-pw" not in captured.err
        assert "admin" in captured.out  # the username is fine to show

    def test_no_ssh_is_attempted(self, inventory_path, monkeypatch) -> None:
        def explode(*args, **kwargs):
            raise AssertionError("--list-devices must not touch the network")

        monkeypatch.setattr(cli.runner, "run", explode)
        assert cli.main(["--list-devices", "-i", str(inventory_path)]) == cli.EXIT_OK


class TestTestConnection:
    def test_all_reachable_exits_zero(self, inventory_path, fake_run) -> None:
        assert cli.main(["--test-connection", "-i", str(inventory_path)]) == cli.EXIT_OK

    def test_any_failure_exits_one(self, inventory_path, fake_run) -> None:
        fake_run["leaf1"] = DeviceStatus.UNREACHABLE
        code = cli.main(["--test-connection", "-i", str(inventory_path)])
        assert code == cli.EXIT_DEVICE_FAILURE

    def test_json_summary(self, inventory_path, fake_run, capsys) -> None:
        fake_run["leaf1"] = DeviceStatus.AUTH_FAILED
        cli.main(["--test-connection", "-i", str(inventory_path), "--json", "--no-color"])

        payload = json.loads(capsys.readouterr().out)
        assert payload["summary"] == {"total": 2, "ok": 1, "failed": 1}
        assert payload["devices"][1]["status"] == "auth_failed"

    def test_output_omitted_from_json_by_default(
        self, inventory_path, fake_run, capsys
    ) -> None:
        cli.main(["--test-connection", "-i", str(inventory_path), "--json", "--no-color"])
        payload = json.loads(capsys.readouterr().out)
        assert "outputs" not in payload["devices"][0]

    def test_show_output_includes_it(self, inventory_path, fake_run, capsys) -> None:
        cli.main(
            [
                "--test-connection",
                "-i",
                str(inventory_path),
                "--json",
                "--show-output",
                "--no-color",
            ]
        )
        payload = json.loads(capsys.readouterr().out)
        assert payload["devices"][0]["outputs"]["show running-config"] == "hostname spine1"


class TestFiltering:
    def test_device_filter(self, inventory_path, fake_run, capsys) -> None:
        cli.main(
            ["--test-connection", "-i", str(inventory_path), "-d", "leaf1", "--json"]
        )
        payload = json.loads(capsys.readouterr().out)
        assert [d["device"] for d in payload["devices"]] == ["leaf1"]

    def test_comma_separated_devices(self, inventory_path, fake_run, capsys) -> None:
        cli.main(
            ["--test-connection", "-i", str(inventory_path), "-d", "leaf1,spine1", "--json"]
        )
        payload = json.loads(capsys.readouterr().out)
        assert len(payload["devices"]) == 2

    def test_tag_filter(self, inventory_path, fake_run, capsys) -> None:
        cli.main(["--test-connection", "-i", str(inventory_path), "--tag", "spine", "--json"])
        payload = json.loads(capsys.readouterr().out)
        assert [d["device"] for d in payload["devices"]] == ["spine1"]

    def test_unknown_device_is_a_config_error(self, inventory_path, fake_run) -> None:
        code = cli.main(["--test-connection", "-i", str(inventory_path), "-d", "nope"])
        assert code == cli.EXIT_CONFIG_ERROR

    def test_tag_matching_nothing_is_a_config_error(self, inventory_path, fake_run) -> None:
        """An empty run must not look like a clean pass."""
        code = cli.main(["--test-connection", "-i", str(inventory_path), "--tag", "nope"])
        assert code == cli.EXIT_CONFIG_ERROR


class TestErrorHandling:
    def test_missing_inventory(self, tmp_path, capsys) -> None:
        code = cli.main(["--list-devices", "-i", str(tmp_path / "nope.yaml")])
        assert code == cli.EXIT_CONFIG_ERROR
        assert "not found" in capsys.readouterr().err

    def test_missing_credentials(self, inventory_path, monkeypatch, capsys) -> None:
        monkeypatch.delenv("NETAUDIT_LAB_PASSWORD", raising=False)
        code = cli.main(["--list-devices", "-i", str(inventory_path)])
        assert code == cli.EXIT_CONFIG_ERROR
        assert "NETAUDIT_LAB_PASSWORD" in capsys.readouterr().err

    def test_malformed_inventory(self, tmp_path, capsys) -> None:
        path = tmp_path / "inventory.yaml"
        path.write_text("devices: [\n - name: x\n", encoding="utf-8")
        assert cli.main(["--list-devices", "-i", str(path)]) == cli.EXIT_CONFIG_ERROR

    def test_an_action_is_required(self) -> None:
        with pytest.raises(SystemExit) as exc:
            cli.main([])
        assert exc.value.code == 2

    def test_actions_are_mutually_exclusive(self, inventory_path) -> None:
        with pytest.raises(SystemExit):
            cli.main(["--list-devices", "--test-connection", "-i", str(inventory_path)])

    def test_command_requires_test_connection(self, inventory_path) -> None:
        with pytest.raises(SystemExit):
            cli.main(
                ["--list-devices", "-i", str(inventory_path), "-c", "show version"]
            )

    @pytest.mark.parametrize("flag,value", [("--workers", "0"), ("--retries", "0")])
    def test_rejects_nonsense_numbers(self, inventory_path, flag, value) -> None:
        with pytest.raises(SystemExit):
            cli.main(["--test-connection", "-i", str(inventory_path), flag, value])
