"""Parse and validate ``inventory.yaml`` into :class:`~netauditor.models.Device`.

Schema::

    defaults:            # optional, merged into every device
      device_type: arista_eos
      credentials: lab
      port: 22

    devices:
      - name: ceos1      # required, unique
        host: 172.20.20.11   # required (IP or DNS name)
        device_type: arista_eos
        tags: [lab, spine]

Validation is deliberately strict: an unknown key is an error rather than a
silent no-op, because a typo like ``devcie_type`` would otherwise fail much
later as a confusing connection error.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import yaml

from . import credentials as creds
from .errors import CredentialError, InventoryError
from .models import Device

log = logging.getLogger(__name__)

#: Keys accepted on a device entry (and in the `defaults` block).
DEVICE_KEYS = frozenset(
    {
        "name",
        "host",
        "device_type",
        "port",
        "tags",
        "credentials",
        "key_file",
        "backup_command",
        "conn_timeout",
        "read_timeout",
        "extra_args",
    }
)

#: `name` and `host` identify a specific device, so they may not be defaulted.
DEFAULTABLE_KEYS = DEVICE_KEYS - {"name", "host"}

TOP_LEVEL_KEYS = frozenset({"defaults", "devices"})

#: Per-platform command that yields the running configuration. Used when a
#: device (or the defaults block) does not set `backup_command` explicitly.
DEFAULT_BACKUP_COMMANDS: dict[str, str] = {
    "arista_eos": "show running-config",
    "cisco_ios": "show running-config",
    "cisco_xe": "show running-config",
    "cisco_xr": "show running-config",
    "cisco_nxos": "show running-config",
    "cisco_asa": "show running-config",
    "juniper_junos": "show configuration | display set",
    "nokia_srlinux": "info",
    "linux": "cat /etc/network/interfaces",
}

FALLBACK_BACKUP_COMMAND = "show running-config"


def _require_mapping(value: Any, what: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise InventoryError(f"{what} must be a mapping, got {type(value).__name__}")
    return value


def _check_keys(entry: Mapping[str, Any], allowed: frozenset[str], what: str) -> None:
    unknown = set(entry) - allowed
    if unknown:
        raise InventoryError(
            f"{what}: unknown key(s) {sorted(unknown)}. "
            f"Allowed keys: {sorted(allowed)}"
        )


def _coerce_int(value: Any, key: str, what: str) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError):
        raise InventoryError(f"{what}: {key} must be an integer, got {value!r}") from None
    if result <= 0:
        raise InventoryError(f"{what}: {key} must be positive, got {result}")
    return result


def _coerce_tags(value: Any, what: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    if isinstance(value, Sequence):
        return tuple(str(tag) for tag in value)
    raise InventoryError(f"{what}: tags must be a string or list of strings")


def load_raw(path: str | Path) -> dict[str, Any]:
    """Read and parse the YAML file, with useful errors for the common mistakes."""
    path = Path(path)
    if not path.is_file():
        raise InventoryError(f"inventory file not found: {path}")

    try:
        with path.open("r", encoding="utf-8") as handle:
            data = yaml.safe_load(handle)
    except yaml.YAMLError as exc:
        raise InventoryError(f"could not parse {path}: {exc}") from exc

    if data is None:
        raise InventoryError(f"{path} is empty")

    data = _require_mapping(data, str(path))
    _check_keys(data, TOP_LEVEL_KEYS, str(path))
    return dict(data)


def load_inventory(
    path: str | Path,
    *,
    env: Mapping[str, str] | None = None,
) -> list[Device]:
    """Load ``inventory.yaml`` and return fully-resolved devices.

    Credentials are resolved here so that a missing secret fails immediately at
    startup rather than halfway through a run.

    Raises:
        InventoryError: file missing, malformed, or schema-invalid.
        CredentialError: a device's credentials are not in the environment.
    """
    data = load_raw(path)

    defaults = _require_mapping(data.get("defaults") or {}, f"{path}: defaults")
    _check_keys(defaults, DEFAULTABLE_KEYS, f"{path}: defaults")

    raw_devices = data.get("devices")
    if not raw_devices:
        raise InventoryError(f"{path}: no devices defined")
    if not isinstance(raw_devices, Sequence) or isinstance(raw_devices, str):
        raise InventoryError(f"{path}: devices must be a list")

    devices: list[Device] = []
    seen: dict[str, int] = {}

    for index, raw in enumerate(raw_devices):
        what = f"{path}: devices[{index}]"
        entry = dict(_require_mapping(raw, what))
        _check_keys(entry, DEVICE_KEYS, what)

        name = entry.get("name")
        if not name or not str(name).strip():
            raise InventoryError(f"{what}: 'name' is required")
        name = str(name).strip()
        what = f"{path}: device {name!r}"

        if name in seen:
            raise InventoryError(
                f"{what}: duplicate name (already defined at devices[{seen[name]}])"
            )
        seen[name] = index

        merged = {**defaults, **entry}

        host = merged.get("host")
        if not host or not str(host).strip():
            raise InventoryError(f"{what}: 'host' is required")
        host = str(host).strip()

        device_type = merged.get("device_type")
        if not device_type:
            raise InventoryError(
                f"{what}: 'device_type' is required "
                f"(set it on the device or in defaults, e.g. arista_eos, cisco_ios)"
            )
        device_type = str(device_type).strip()

        profile = merged.get("credentials")
        profile = str(profile).strip() if profile else None

        try:
            resolved = creds.resolve(
                name,
                profile,
                env=env,
                key_file=merged.get("key_file"),
            )
        except CredentialError as exc:
            # Re-raise with the inventory location attached.
            raise CredentialError(f"{what}: {exc}") from exc

        backup_command = merged.get("backup_command") or DEFAULT_BACKUP_COMMANDS.get(
            device_type, FALLBACK_BACKUP_COMMAND
        )

        extra_args = merged.get("extra_args") or {}
        if not isinstance(extra_args, Mapping):
            raise InventoryError(f"{what}: extra_args must be a mapping")

        devices.append(
            Device(
                name=name,
                host=host,
                device_type=device_type,
                credentials=resolved,
                port=_coerce_int(merged.get("port", 22), "port", what),
                tags=_coerce_tags(merged.get("tags"), what),
                backup_command=str(backup_command),
                conn_timeout=_coerce_int(
                    merged.get("conn_timeout", 10), "conn_timeout", what
                ),
                read_timeout=_coerce_int(
                    merged.get("read_timeout", 30), "read_timeout", what
                ),
                extra_args=dict(extra_args),
            )
        )

    log.debug("loaded %d device(s) from %s", len(devices), path)
    return devices


def filter_devices(
    devices: Iterable[Device],
    *,
    names: Sequence[str] | None = None,
    tags: Sequence[str] | None = None,
) -> list[Device]:
    """Narrow a device list by name and/or tag.

    Both filters are OR-within, AND-between: ``names=[a, b], tags=[edge]``
    means "named a or b, *and* tagged edge". Name matching is
    case-insensitive; an unmatched name is an error rather than a silent
    empty run.
    """
    result = list(devices)

    if names:
        wanted = {n.strip().lower() for n in names if n.strip()}
        known = {d.name.lower() for d in result}
        missing = sorted(wanted - known)
        if missing:
            raise InventoryError(
                f"no device in inventory named: {', '.join(missing)}. "
                f"Known devices: {', '.join(sorted(d.name for d in result))}"
            )
        result = [d for d in result if d.name.lower() in wanted]

    if tags:
        wanted_tags = {t.strip().lower() for t in tags if t.strip()}
        result = [
            d for d in result if wanted_tags & {t.lower() for t in d.tags}
        ]

    return result
