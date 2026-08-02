"""HTML and Markdown report rendering.

The report is what a reviewer actually reads, so the things tested here are:
it contains the findings, it escapes device-supplied text, and it stands alone
with no network dependencies.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone

import pytest

from netauditor import render
from netauditor.models import (
    ComplianceResult,
    RuleResult,
    RuleStatus,
    Severity,
    Violation,
)

WHEN = datetime(2026, 8, 2, 4, 5, 6, tzinfo=timezone.utc)


def failing(device, **kwargs) -> ComplianceResult:
    return ComplianceResult(
        device=device,
        source="backups/ceos-spine1/20260802T032349Z.cfg",
        results=[
            RuleResult(
                rule_name="Spines must route",
                status=RuleStatus.FAIL,
                severity=Severity.HIGH,
                description="A spine with ip routing disabled is an expensive switch.",
                violations=(
                    Violation(kind="missing", expected="ip routing"),
                    Violation(
                        kind="forbidden",
                        expected="no ip routing",
                        found="no ip routing",
                        line_number=38,
                    ),
                ),
            ),
            RuleResult(
                rule_name="AAA root disabled",
                status=RuleStatus.PASS,
                severity=Severity.HIGH,
            ),
        ],
        **kwargs,
    )


def passing(device) -> ComplianceResult:
    return ComplianceResult(
        device=device,
        results=[
            RuleResult(
                rule_name="AAA root disabled",
                status=RuleStatus.PASS,
                severity=Severity.HIGH,
            )
        ],
    )


class TestMarkdown:
    def test_includes_summary_and_device(self, device) -> None:
        out = render.to_markdown([failing(device)], when=WHEN)
        assert "0/1 devices compliant" in out
        assert "ceos-spine1" in out
        assert "2026-08-02 04:05:06 UTC" in out

    def test_lists_each_violation(self, device) -> None:
        out = render.to_markdown([failing(device)], when=WHEN)
        assert "missing: ip routing" in out
        assert "forbidden: no ip routing (line 38)" in out

    def test_passing_rules_are_not_listed_as_findings(self, device) -> None:
        out = render.to_markdown([failing(device)], when=WHEN)
        assert out.count("AAA root disabled") == 0

    def test_all_compliant_says_so(self, device) -> None:
        out = render.to_markdown([passing(device)], when=WHEN)
        assert "All devices are compliant." in out
        assert "## Findings" not in out

    def test_error_device_is_reported(self, device) -> None:
        result = ComplianceResult(device=device, error="could not open TCP session")
        out = render.to_markdown([result], when=WHEN)
        assert "could not open TCP session" in out

    def test_severity_breakdown(self, device) -> None:
        out = render.to_markdown([failing(device)], when=WHEN)
        assert "| high | 2 |" in out


class TestHtml:
    def test_is_a_complete_document(self, device) -> None:
        out = render.to_html([failing(device)], when=WHEN)
        assert out.startswith("<!DOCTYPE html>")
        assert out.rstrip().endswith("</html>")

    def test_is_self_contained(self, device) -> None:
        """A report that needs a CDN is useless once emailed or archived, or on
        a management network with no internet access."""
        out = render.to_html([failing(device)], when=WHEN)
        for external in ("http://", "https://", "<script", "src="):
            assert external not in out

    def test_includes_findings(self, device) -> None:
        out = render.to_html([failing(device)], when=WHEN)
        assert "Spines must route" in out
        assert "forbidden: no ip routing (line 38)" in out

    def test_all_compliant_shows_empty_state(self, device) -> None:
        out = render.to_html([passing(device)], when=WHEN)
        assert "All devices are compliant." in out

    def test_escapes_device_supplied_text(self, device) -> None:
        """Config content reaches the report; it must not reach it as markup."""
        result = ComplianceResult(
            device=device,
            results=[
                RuleResult(
                    rule_name="No scripts",
                    status=RuleStatus.FAIL,
                    severity=Severity.HIGH,
                    violations=(
                        Violation(
                            kind="forbidden",
                            expected="x",
                            found="<script>alert(1)</script>",
                            line_number=4,
                        ),
                    ),
                )
            ],
        )
        out = render.to_html([result], when=WHEN)
        assert "<script>alert(1)</script>" not in out
        assert "&lt;script&gt;" in out

    def test_escapes_device_name(self, device) -> None:
        evil = replace(device, name="r1<img onerror=x>")
        out = render.to_html([passing(evil)], when=WHEN)
        assert "<img onerror" not in out

    def test_counts_are_rendered(self, device) -> None:
        out = render.to_html([failing(device), passing(device)], when=WHEN)
        assert "Devices" in out and "Compliant" in out and "Violations" in out


class TestCliIntegration:
    @pytest.mark.parametrize("name", ["report.html", "report.md", "report.json"])
    def test_extension_selects_format(self, device, tmp_path, name) -> None:
        from netauditor import cli

        path = cli._write_report(str(tmp_path / name), [failing(device)])
        assert path.is_file()
        assert path.read_text(encoding="utf-8").strip()

    def test_unknown_extension_is_an_error(self, device, tmp_path) -> None:
        from netauditor import cli
        from netauditor.errors import InventoryError

        with pytest.raises(InventoryError, match="cannot infer report format"):
            cli._write_report(str(tmp_path / "report.docx"), [failing(device)])

    def test_creates_parent_directory(self, device, tmp_path) -> None:
        from netauditor import cli

        path = cli._write_report(str(tmp_path / "a" / "b" / "r.md"), [failing(device)])
        assert path.is_file()
