"""Evaluate device configs against the golden rules.

Matching semantics, chosen for predictability over convenience:

``required`` / ``forbidden``
    Exact match against a *whitespace-normalized* config line. Indentation and
    repeated spaces are ignored, so ``   ntp server  10.0.0.1`` matches
    ``ntp server 10.0.0.1``, but ``ntp server 10.0.0.1`` does **not** match a
    device line of ``ntp server 10.0.0.1 iburst``.

``required_regex`` / ``forbidden_regex``
    Searched against each normalized line, so ``^ntp server `` covers the
    partial-match case above.

Substring matching was deliberately not used. In a compliance tool the
expensive failure is a rule that silently passes, and ``forbidden: ["ip http
server"]`` matching-by-substring would be satisfied by a *comment* mentioning
it. Exact-or-regex means a rule either matches what you meant or visibly does
not.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Iterable, Sequence

from . import backup as backup_mod
from .golden import GoldenConfig, Rule
from .models import (
    ComplianceResult,
    Device,
    DeviceResult,
    RuleResult,
    RuleStatus,
    Violation,
)

log = logging.getLogger(__name__)

_WHITESPACE = re.compile(r"\s+")


def normalize_line(line: str) -> str:
    """Collapse whitespace so indentation does not affect matching."""
    return _WHITESPACE.sub(" ", line).strip()


def config_lines(config: str) -> list[tuple[int, str]]:
    """Normalized, meaningful config lines as ``(line_number, text)``.

    Blank lines and bare ``!`` separators are dropped -- they carry no
    configuration and would only add noise to line numbers in violations.
    Line numbers refer to the original file, so a violation points at
    something a human can find.
    """
    out: list[tuple[int, str]] = []
    for number, raw in enumerate(config.splitlines(), start=1):
        text = normalize_line(raw)
        if not text or text == "!":
            continue
        out.append((number, text))
    return out


def evaluate_rule(rule: Rule, lines: Sequence[tuple[int, str]]) -> RuleResult:
    """Check one rule against one config."""
    present = {text for _, text in lines}
    violations: list[Violation] = []

    for expected in rule.required:
        if normalize_line(expected) not in present:
            violations.append(Violation(kind="missing", expected=expected))

    for pattern in rule.required_regex:
        if not any(pattern.search(text) for _, text in lines):
            violations.append(Violation(kind="missing", expected=pattern.pattern))

    for banned in rule.forbidden:
        target = normalize_line(banned)
        for number, text in lines:
            if text == target:
                violations.append(
                    Violation(
                        kind="forbidden",
                        expected=banned,
                        found=text,
                        line_number=number,
                    )
                )

    for pattern in rule.forbidden_regex:
        for number, text in lines:
            if pattern.search(text):
                violations.append(
                    Violation(
                        kind="forbidden",
                        expected=pattern.pattern,
                        found=text,
                        line_number=number,
                    )
                )

    return RuleResult(
        rule_name=rule.name,
        status=RuleStatus.FAIL if violations else RuleStatus.PASS,
        severity=rule.severity,
        violations=tuple(violations),
        description=rule.description,
    )


def evaluate(
    device: Device,
    config: str,
    golden: GoldenConfig,
    *,
    source: str | None = None,
) -> ComplianceResult:
    """Evaluate every applicable rule against one device's config."""
    lines = config_lines(config)
    results: list[RuleResult] = []

    for rule in golden.rules:
        if not rule.applies_to(device):
            results.append(
                RuleResult(
                    rule_name=rule.name,
                    status=RuleStatus.NOT_APPLICABLE,
                    severity=rule.severity,
                    description=rule.description,
                )
            )
            continue
        results.append(evaluate_rule(rule, lines))

    return ComplianceResult(device=device, results=results, source=source)


def audit_from_backups(
    devices: Iterable[Device],
    golden: GoldenConfig,
    backup_root: Path | str,
) -> list[ComplianceResult]:
    """Audit each device's most recent backup.

    Auditing what was backed up is reproducible and works with no network,
    which is what makes the check runnable in CI.
    """
    results: list[ComplianceResult] = []

    for device in devices:
        path = backup_mod.latest_backup(backup_mod.device_dir(backup_root, device))
        if path is None:
            results.append(
                ComplianceResult(
                    device=device,
                    error=f"no backup found under {backup_mod.device_dir(backup_root, device)}",
                )
            )
            continue

        try:
            config = path.read_text(encoding="utf-8")
        except OSError as exc:
            results.append(
                ComplianceResult(device=device, error=f"could not read {path}: {exc}")
            )
            continue

        results.append(evaluate(device, config, golden, source=str(path)))

    return results


def audit_from_results(
    device_results: Sequence[DeviceResult],
    golden: GoldenConfig,
) -> list[ComplianceResult]:
    """Audit configs just collected from the devices themselves.

    A device that could not be collected becomes a ComplianceResult carrying
    the error -- never a silent pass, and never dropped from the report.
    """
    results: list[ComplianceResult] = []

    for result in device_results:
        if not result.ok:
            results.append(
                ComplianceResult(
                    device=result.device,
                    error=result.error or "collection failed",
                )
            )
            continue

        config = result.outputs.get(result.device.backup_command)
        if config is None:
            results.append(
                ComplianceResult(
                    device=result.device,
                    error=f"no output for {result.device.backup_command!r}",
                )
            )
            continue

        results.append(
            evaluate(result.device, config, golden, source="live")
        )

    return results
