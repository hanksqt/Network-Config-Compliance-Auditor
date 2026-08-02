"""Core data types shared across the auditor.

Everything the tool passes around is a plain dataclass so it can be built in
tests without touching the network.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any


class DeviceStatus(str, Enum):
    """Outcome of talking to a single device.

    Subclasses ``str`` so it serializes straight to JSON and compares against
    plain strings in tests.
    """

    SUCCESS = "success"
    UNREACHABLE = "unreachable"
    AUTH_FAILED = "auth_failed"
    TIMEOUT = "timeout"
    COMMAND_FAILED = "command_failed"
    CONFIG_ERROR = "config_error"
    ERROR = "error"

    @property
    def is_ok(self) -> bool:
        return self is DeviceStatus.SUCCESS


#: Statuses worth retrying. Auth failures and bad inventory entries will not
#: fix themselves, so retrying them just slows the run down.
RETRYABLE_STATUSES = frozenset(
    {DeviceStatus.UNREACHABLE, DeviceStatus.TIMEOUT}
)


@dataclass(frozen=True)
class Credentials:
    """Resolved credentials for one device.

    ``repr=False`` on the secret fields keeps passwords out of tracebacks and
    debug logs, which is the whole reason this is a separate type.
    """

    username: str
    password: str | None = field(default=None, repr=False)
    enable_secret: str | None = field(default=None, repr=False)
    key_file: str | None = None

    @property
    def uses_key(self) -> bool:
        return bool(self.key_file)


@dataclass(frozen=True)
class Device:
    """One device from inventory.yaml, with credentials already resolved."""

    name: str
    host: str
    device_type: str
    credentials: Credentials = field(repr=False)
    port: int = 22
    tags: tuple[str, ...] = ()
    # Command whose output is treated as the running config for this platform.
    backup_command: str = "show running-config"
    conn_timeout: int = 10
    read_timeout: int = 30
    # Free-form passthrough to netmiko.ConnectHandler for anything the schema
    # does not model explicitly (e.g. ssh_config_file, global_delay_factor).
    extra_args: dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def label(self) -> str:
        """Human-readable identifier used in logs and reports."""
        return f"{self.name} ({self.host}:{self.port})"

    def netmiko_params(self) -> dict[str, Any]:
        """Build the kwargs for ``netmiko.ConnectHandler``."""
        params: dict[str, Any] = {
            "device_type": self.device_type,
            "host": self.host,
            "port": self.port,
            "username": self.credentials.username,
            "conn_timeout": self.conn_timeout,
            # Give banner/auth the same budget as the TCP connect so a slow
            # container does not look like an auth failure.
            "banner_timeout": max(self.conn_timeout, 15),
            "auth_timeout": max(self.conn_timeout, 15),
        }
        if self.credentials.uses_key:
            params["use_keys"] = True
            params["key_file"] = self.credentials.key_file
            # Some platforms still want a password alongside the key.
            if self.credentials.password:
                params["password"] = self.credentials.password
        else:
            params["password"] = self.credentials.password

        if self.credentials.enable_secret:
            params["secret"] = self.credentials.enable_secret

        params.update(self.extra_args)
        return params


class Severity(str, Enum):
    """How much a failed rule matters. Drives report ordering and exit codes."""

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"

    @property
    def rank(self) -> int:
        return {"high": 0, "medium": 1, "low": 2}[self.value]


class RuleStatus(str, Enum):
    PASS = "pass"
    FAIL = "fail"
    #: Rule does not apply to this device (tag scoping).
    NOT_APPLICABLE = "not_applicable"


@dataclass(frozen=True)
class Violation:
    """One specific reason a rule failed, naming the line involved."""

    #: "missing" (required line absent) or "forbidden" (banned line present)
    kind: str
    #: The pattern from the golden file.
    expected: str
    #: The offending config line, for forbidden matches.
    found: str | None = None
    #: 1-indexed line number in the config, for forbidden matches.
    line_number: int | None = None

    def describe(self) -> str:
        if self.kind == "missing":
            return f"missing: {self.expected}"
        location = f" (line {self.line_number})" if self.line_number else ""
        return f"forbidden: {self.found or self.expected}{location}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "expected": self.expected,
            "found": self.found,
            "line_number": self.line_number,
        }


@dataclass
class RuleResult:
    """Outcome of evaluating one rule against one device."""

    rule_name: str
    status: RuleStatus
    severity: Severity = Severity.HIGH
    violations: tuple[Violation, ...] = ()
    description: str | None = None

    @property
    def failed(self) -> bool:
        return self.status is RuleStatus.FAIL

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule": self.rule_name,
            "status": self.status.value,
            "severity": self.severity.value,
            "description": self.description,
            "violations": [v.to_dict() for v in self.violations],
        }


@dataclass
class ComplianceResult:
    """Every rule evaluated against one device."""

    device: Device
    results: list[RuleResult] = field(default_factory=list)
    #: Set when the config could not be obtained at all.
    error: str | None = None
    #: Where the audited config came from, for reproducibility.
    source: str | None = None

    @property
    def evaluated(self) -> list[RuleResult]:
        return [r for r in self.results if r.status is not RuleStatus.NOT_APPLICABLE]

    @property
    def failures(self) -> list[RuleResult]:
        """Failed rules, most severe first."""
        return sorted(
            (r for r in self.results if r.failed),
            key=lambda r: r.severity.rank,
        )

    @property
    def compliant(self) -> bool:
        """A device with no config to audit is not compliant -- it is unknown,
        and unknown must not read as a pass."""
        return self.error is None and not self.failures

    @property
    def violation_count(self) -> int:
        return sum(len(r.violations) for r in self.failures)

    def to_dict(self) -> dict[str, Any]:
        return {
            "device": self.device.name,
            "host": self.device.host,
            "tags": list(self.device.tags),
            "compliant": self.compliant,
            "error": self.error,
            "source": self.source,
            "rules_evaluated": len(self.evaluated),
            "rules_failed": len(self.failures),
            "violations": self.violation_count,
            "results": [r.to_dict() for r in self.results],
        }


class BackupStatus(str, Enum):
    """Outcome of writing one device's config to disk."""

    WRITTEN = "written"
    #: Config is byte-identical to the previous capture, so nothing was written.
    UNCHANGED = "unchanged"
    #: Collection failed, so there was nothing to write.
    SKIPPED = "skipped"
    #: Collection succeeded but the config failed a sanity check.
    REJECTED = "rejected"
    #: The write itself failed (permissions, disk).
    FAILED = "failed"

    @property
    def is_ok(self) -> bool:
        return self in (BackupStatus.WRITTEN, BackupStatus.UNCHANGED)


@dataclass
class BackupResult:
    """What happened when one device's config was written."""

    device: Device
    status: BackupStatus
    path: Path | None = None
    error: str | None = None
    lines: int = 0

    @property
    def ok(self) -> bool:
        return self.status.is_ok

    def to_dict(self) -> dict[str, Any]:
        return {
            "device": self.device.name,
            "status": self.status.value,
            "ok": self.ok,
            "path": str(self.path) if self.path else None,
            "lines": self.lines,
            "error": self.error,
        }


@dataclass
class DeviceResult:
    """What happened when the auditor talked to one device."""

    device: Device
    status: DeviceStatus
    #: command -> raw output, populated only on success
    outputs: dict[str, str] = field(default_factory=dict)
    error: str | None = None
    duration_s: float = 0.0
    attempts: int = 1

    @property
    def ok(self) -> bool:
        return self.status.is_ok

    def to_dict(self, *, include_output: bool = False) -> dict[str, Any]:
        """JSON-safe representation. Output is opt-in because configs are big."""
        payload: dict[str, Any] = {
            "device": self.device.name,
            "host": self.device.host,
            "port": self.device.port,
            "device_type": self.device.device_type,
            "tags": list(self.device.tags),
            "status": self.status.value,
            "ok": self.ok,
            "error": self.error,
            "duration_s": round(self.duration_s, 3),
            "attempts": self.attempts,
        }
        if include_output:
            payload["outputs"] = self.outputs
        return payload
