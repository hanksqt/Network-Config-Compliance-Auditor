"""inventory.yaml parsing, validation, defaults merging, and filtering."""

from __future__ import annotations

import pytest

from netauditor import inventory
from netauditor.errors import CredentialError, InventoryError


def base_inventory(**overrides) -> dict:
    data = {
        "defaults": {"device_type": "arista_eos", "credentials": "lab"},
        "devices": [
            {"name": "spine1", "host": "172.20.20.11", "tags": ["lab", "spine"]},
            {"name": "leaf1", "host": "172.20.20.12", "tags": ["lab", "leaf"]},
        ],
    }
    data.update(overrides)
    return data


class TestLoading:
    def test_loads_devices(self, write_inventory, lab_env) -> None:
        path = write_inventory(base_inventory())
        devices = inventory.load_inventory(path, env=lab_env)

        assert [d.name for d in devices] == ["spine1", "leaf1"]
        assert devices[0].host == "172.20.20.11"
        assert devices[0].credentials.username == "admin"

    def test_defaults_are_merged(self, write_inventory, lab_env) -> None:
        path = write_inventory(base_inventory())
        devices = inventory.load_inventory(path, env=lab_env)
        assert all(d.device_type == "arista_eos" for d in devices)
        assert all(d.port == 22 for d in devices)

    def test_device_overrides_defaults(self, write_inventory, lab_env) -> None:
        data = base_inventory()
        data["devices"][1]["device_type"] = "cisco_ios"
        data["devices"][1]["port"] = 2222

        devices = inventory.load_inventory(write_inventory(data), env=lab_env)
        assert devices[0].device_type == "arista_eos"
        assert devices[1].device_type == "cisco_ios"
        assert devices[1].port == 2222

    def test_tags_accept_a_bare_string(self, write_inventory, lab_env) -> None:
        data = base_inventory()
        data["devices"][0]["tags"] = "lab"
        devices = inventory.load_inventory(write_inventory(data), env=lab_env)
        assert devices[0].tags == ("lab",)

    def test_extra_args_passed_through(self, write_inventory, lab_env) -> None:
        data = base_inventory()
        data["devices"][0]["extra_args"] = {"global_delay_factor": 2}
        devices = inventory.load_inventory(write_inventory(data), env=lab_env)
        assert devices[0].netmiko_params()["global_delay_factor"] == 2


class TestBackupCommand:
    def test_every_platform_key_is_a_real_netmiko_driver(self) -> None:
        """A typo'd key here is invisible: it silently falls back to the generic
        `show running-config`, which is wrong on most non-Cisco platforms."""
        from netmiko.ssh_dispatcher import CLASS_MAPPER_BASE

        unknown = sorted(
            set(inventory.DEFAULT_BACKUP_COMMANDS) - set(CLASS_MAPPER_BASE)
        )
        assert not unknown, f"not Netmiko platform names: {unknown}"

    def test_platform_default_is_used(self, write_inventory, lab_env) -> None:
        data = base_inventory()
        data["devices"][1]["device_type"] = "juniper_junos"
        devices = inventory.load_inventory(write_inventory(data), env=lab_env)

        assert devices[0].backup_command == "show running-config"
        assert devices[1].backup_command == "show configuration | display set"

    def test_explicit_command_wins(self, write_inventory, lab_env) -> None:
        data = base_inventory()
        data["devices"][0]["backup_command"] = "show running-config all"
        devices = inventory.load_inventory(write_inventory(data), env=lab_env)
        assert devices[0].backup_command == "show running-config all"

    def test_unknown_platform_falls_back(self, write_inventory, lab_env) -> None:
        data = base_inventory()
        data["devices"][0]["device_type"] = "some_new_vendor"
        devices = inventory.load_inventory(write_inventory(data), env=lab_env)
        assert devices[0].backup_command == inventory.FALLBACK_BACKUP_COMMAND


class TestValidation:
    def test_missing_file(self, tmp_path) -> None:
        with pytest.raises(InventoryError, match="not found"):
            inventory.load_inventory(tmp_path / "nope.yaml")

    def test_empty_file(self, tmp_path) -> None:
        path = tmp_path / "inventory.yaml"
        path.write_text("", encoding="utf-8")
        with pytest.raises(InventoryError, match="empty"):
            inventory.load_inventory(path)

    def test_malformed_yaml(self, tmp_path) -> None:
        path = tmp_path / "inventory.yaml"
        path.write_text("devices: [\n  - name: x\n", encoding="utf-8")
        with pytest.raises(InventoryError, match="could not parse"):
            inventory.load_inventory(path)

    def test_no_devices(self, write_inventory) -> None:
        with pytest.raises(InventoryError, match="no devices"):
            inventory.load_inventory(write_inventory({"devices": []}))

    def test_typo_in_key_is_an_error(self, write_inventory, lab_env) -> None:
        """A silently-ignored typo would surface much later as a weird SSH bug."""
        data = base_inventory()
        data["devices"][0]["devcie_type"] = "arista_eos"
        with pytest.raises(InventoryError, match="devcie_type"):
            inventory.load_inventory(write_inventory(data), env=lab_env)

    def test_unknown_top_level_key(self, write_inventory) -> None:
        with pytest.raises(InventoryError, match="golden"):
            inventory.load_inventory(write_inventory(base_inventory(golden={})))

    def test_duplicate_name(self, write_inventory, lab_env) -> None:
        data = base_inventory()
        data["devices"][1]["name"] = "spine1"
        with pytest.raises(InventoryError, match="duplicate"):
            inventory.load_inventory(write_inventory(data), env=lab_env)

    def test_missing_host(self, write_inventory, lab_env) -> None:
        data = base_inventory()
        del data["devices"][0]["host"]
        with pytest.raises(InventoryError, match="'host' is required"):
            inventory.load_inventory(write_inventory(data), env=lab_env)

    def test_missing_name(self, write_inventory, lab_env) -> None:
        data = base_inventory()
        del data["devices"][0]["name"]
        with pytest.raises(InventoryError, match="'name' is required"):
            inventory.load_inventory(write_inventory(data), env=lab_env)

    def test_missing_device_type(self, write_inventory, lab_env) -> None:
        data = base_inventory()
        del data["defaults"]["device_type"]
        with pytest.raises(InventoryError, match="'device_type' is required"):
            inventory.load_inventory(write_inventory(data), env=lab_env)

    def test_name_and_host_cannot_be_defaulted(self, write_inventory, lab_env) -> None:
        data = base_inventory()
        data["defaults"]["host"] = "10.0.0.1"
        with pytest.raises(InventoryError, match="host"):
            inventory.load_inventory(write_inventory(data), env=lab_env)

    def test_bad_port(self, write_inventory, lab_env) -> None:
        data = base_inventory()
        data["devices"][0]["port"] = "twenty-two"
        with pytest.raises(InventoryError, match="port must be an integer"):
            inventory.load_inventory(write_inventory(data), env=lab_env)

    def test_negative_timeout(self, write_inventory, lab_env) -> None:
        data = base_inventory()
        data["devices"][0]["conn_timeout"] = -1
        with pytest.raises(InventoryError, match="must be positive"):
            inventory.load_inventory(write_inventory(data), env=lab_env)

    def test_missing_credentials_mentions_the_device(self, write_inventory) -> None:
        with pytest.raises(CredentialError) as exc:
            inventory.load_inventory(write_inventory(base_inventory()), env={})
        assert "spine1" in str(exc.value)
        assert "NETAUDIT_LAB_USERNAME" in str(exc.value)


class TestFiltering:
    @pytest.fixture
    def devices(self, write_inventory, lab_env):
        data = base_inventory()
        data["devices"].append(
            {"name": "leaf2", "host": "172.20.20.13", "tags": ["lab", "leaf"]}
        )
        return inventory.load_inventory(write_inventory(data), env=lab_env)

    def test_no_filters_returns_everything(self, devices) -> None:
        assert len(inventory.filter_devices(devices)) == 3

    def test_by_name(self, devices) -> None:
        result = inventory.filter_devices(devices, names=["leaf1"])
        assert [d.name for d in result] == ["leaf1"]

    def test_name_is_case_insensitive(self, devices) -> None:
        assert len(inventory.filter_devices(devices, names=["LEAF1"])) == 1

    def test_by_tag(self, devices) -> None:
        result = inventory.filter_devices(devices, tags=["leaf"])
        assert [d.name for d in result] == ["leaf1", "leaf2"]

    def test_name_and_tag_are_and_ed(self, devices) -> None:
        result = inventory.filter_devices(devices, names=["spine1", "leaf1"], tags=["leaf"])
        assert [d.name for d in result] == ["leaf1"]

    def test_unknown_name_is_an_error(self, devices) -> None:
        """Silently running against zero devices would look like a clean pass."""
        with pytest.raises(InventoryError, match="nosuchdevice"):
            inventory.filter_devices(devices, names=["nosuchdevice"])

    def test_unknown_tag_yields_nothing(self, devices) -> None:
        assert inventory.filter_devices(devices, tags=["nope"]) == []
