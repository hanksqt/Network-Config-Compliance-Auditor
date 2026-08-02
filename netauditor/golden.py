"""Parse the golden config: the rules that define "compliant".

The whole point of this file existing is that the rules are *data*. A
compliance tool with its rules hardcoded audits exactly one network; with them
in YAML, the same tool audits lab and production from different files, and a
network engineer who does not write Python can change what compliant means.

Schema::

    defaults:
      severity: high          # applied to rules that omit it

    rules:
      - name: NTP configured
        description: why this matters   # optional, shown in reports
        severity: high                  # high | medium | low
        tags: [spine]                   # only devices with these tags; omit = all
        required:        ["ntp server 10.0.0.1"]
        forbidden:       ["ip http server"]
        required_regex:  ["^ntp server "]
        forbidden_regex: ["^snmp-server community (public|private)"]

Validation is strict for the same reason as the inventory: a typo in
``forbiden`` would silently make a security rule vacuous, and a compliance
tool that quietly checks nothing is worse than no tool at all.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

import yaml

from .errors import AuditorError
from .models import Device, Severity

log = logging.getLogger(__name__)

RULE_KEYS = frozenset(
    {
        "name",
        "description",
        "severity",
        "tags",
        "required",
        "forbidden",
        "required_regex",
        "forbidden_regex",
    }
)

MATCH_KEYS = ("required", "forbidden", "required_regex", "forbidden_regex")

TOP_LEVEL_KEYS = frozenset({"defaults", "rules"})
DEFAULTS_KEYS = frozenset({"severity", "tags"})


class GoldenError(AuditorError):
    """golden.yaml is missing, malformed, or fails validation."""


@dataclass(frozen=True)
class Rule:
    """One compliance rule, already compiled and ready to evaluate."""

    name: str
    severity: Severity = Severity.HIGH
    description: str | None = None
    tags: tuple[str, ...] = ()
    required: tuple[str, ...] = ()
    forbidden: tuple[str, ...] = ()
    required_regex: tuple[re.Pattern[str], ...] = ()
    forbidden_regex: tuple[re.Pattern[str], ...] = ()

    def applies_to(self, device: Device) -> bool:
        """Untagged rules apply everywhere; tagged rules need one tag in common."""
        if not self.tags:
            return True
        wanted = {t.lower() for t in self.tags}
        return bool(wanted & {t.lower() for t in device.tags})


@dataclass
class GoldenConfig:
    rules: list[Rule] = field(default_factory=list)

    def for_device(self, device: Device) -> list[Rule]:
        return [rule for rule in self.rules if rule.applies_to(device)]


def _as_str_tuple(value: Any, what: str, key: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    if isinstance(value, Sequence):
        out = []
        for item in value:
            if not isinstance(item, (str, int, float)):
                raise GoldenError(f"{what}: {key} entries must be strings")
            out.append(str(item))
        return tuple(out)
    raise GoldenError(f"{what}: {key} must be a string or a list of strings")


def _compile(patterns: Sequence[str], what: str, key: str) -> tuple[re.Pattern[str], ...]:
    compiled = []
    for pattern in patterns:
        try:
            compiled.append(re.compile(pattern))
        except re.error as exc:
            raise GoldenError(f"{what}: {key} has an invalid regex {pattern!r}: {exc}")
    return tuple(compiled)


def _severity(value: Any, what: str) -> Severity:
    if value is None:
        return Severity.HIGH
    try:
        return Severity(str(value).strip().lower())
    except ValueError:
        raise GoldenError(
            f"{what}: severity must be one of "
            f"{', '.join(s.value for s in Severity)}, got {value!r}"
        ) from None


def load_golden(path: str | Path) -> GoldenConfig:
    """Load and validate the golden config file.

    Raises:
        GoldenError: file missing, malformed, or schema-invalid.
    """
    path = Path(path)
    if not path.is_file():
        raise GoldenError(f"golden config not found: {path}")

    try:
        with path.open("r", encoding="utf-8") as handle:
            data = yaml.safe_load(handle)
    except yaml.YAMLError as exc:
        raise GoldenError(f"could not parse {path}: {exc}") from exc

    if data is None:
        raise GoldenError(f"{path} is empty")
    if not isinstance(data, Mapping):
        raise GoldenError(f"{path} must be a mapping")

    unknown = set(data) - TOP_LEVEL_KEYS
    if unknown:
        raise GoldenError(
            f"{path}: unknown key(s) {sorted(unknown)}. "
            f"Allowed: {sorted(TOP_LEVEL_KEYS)}"
        )

    defaults = data.get("defaults") or {}
    if not isinstance(defaults, Mapping):
        raise GoldenError(f"{path}: defaults must be a mapping")
    unknown_defaults = set(defaults) - DEFAULTS_KEYS
    if unknown_defaults:
        raise GoldenError(
            f"{path}: defaults has unknown key(s) {sorted(unknown_defaults)}"
        )

    raw_rules = data.get("rules")
    if not raw_rules:
        raise GoldenError(f"{path}: no rules defined")
    if not isinstance(raw_rules, Sequence) or isinstance(raw_rules, str):
        raise GoldenError(f"{path}: rules must be a list")

    rules: list[Rule] = []
    seen: set[str] = set()

    for index, raw in enumerate(raw_rules):
        what = f"{path}: rules[{index}]"
        if not isinstance(raw, Mapping):
            raise GoldenError(f"{what} must be a mapping")

        unknown_keys = set(raw) - RULE_KEYS
        if unknown_keys:
            raise GoldenError(
                f"{what}: unknown key(s) {sorted(unknown_keys)}. "
                f"Allowed: {sorted(RULE_KEYS)}"
            )

        name = str(raw.get("name") or "").strip()
        if not name:
            raise GoldenError(f"{what}: 'name' is required")
        what = f"{path}: rule {name!r}"

        if name in seen:
            raise GoldenError(f"{what}: duplicate rule name")
        seen.add(name)

        matchers = {key: _as_str_tuple(raw.get(key), what, key) for key in MATCH_KEYS}
        if not any(matchers.values()):
            # A rule that checks nothing always passes, which is the most
            # dangerous possible bug in a compliance tool.
            raise GoldenError(
                f"{what}: defines no checks. Add at least one of "
                f"{', '.join(MATCH_KEYS)}."
            )

        rules.append(
            Rule(
                name=name,
                severity=_severity(raw.get("severity", defaults.get("severity")), what),
                description=(
                    str(raw["description"]).strip() if raw.get("description") else None
                ),
                tags=_as_str_tuple(
                    raw.get("tags", defaults.get("tags")), what, "tags"
                ),
                required=matchers["required"],
                forbidden=matchers["forbidden"],
                required_regex=_compile(
                    matchers["required_regex"], what, "required_regex"
                ),
                forbidden_regex=_compile(
                    matchers["forbidden_regex"], what, "forbidden_regex"
                ),
            )
        )

    log.debug("loaded %d rule(s) from %s", len(rules), path)
    return GoldenConfig(rules=rules)
