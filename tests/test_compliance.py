"""Rule evaluation: the part that decides pass or fail.

A false pass here is the worst outcome the tool can produce — it reports
"compliant" on a device that is not — so the negative cases carry most of the
weight.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from netauditor import compliance
from netauditor.golden import GoldenConfig, Rule
from netauditor.models import (
    DeviceResult,
    DeviceStatus,
    RuleStatus,
    Severity,
)

CONFIG = """\
! Command: show running-config
!
no aaa root
!
username admin privilege 15 role network-admin secret sha512 $6$abc
!
hostname ceos-spine1
!
   ip routing
!
banner motd
LEGAL NOTICE
EOF
!
end
"""


def rules(*rs: Rule) -> GoldenConfig:
    return GoldenConfig(rules=list(rs))


class TestNormalization:
    def test_indentation_is_ignored(self) -> None:
        assert compliance.normalize_line("   ip routing") == "ip routing"

    def test_repeated_spaces_collapse(self) -> None:
        assert compliance.normalize_line("ntp  server   10.0.0.1") == (
            "ntp server 10.0.0.1"
        )

    def test_separators_and_blanks_are_dropped(self) -> None:
        lines = compliance.config_lines("!\n\nhostname r1\n!\n")
        assert [text for _, text in lines] == ["hostname r1"]

    def test_line_numbers_refer_to_the_original_file(self) -> None:
        """A violation has to point at something a human can find."""
        lines = compliance.config_lines("!\n!\nip telnet server enable\n")
        assert lines == [(3, "ip telnet server enable")]


class TestRequired:
    def test_present_line_passes(self, device) -> None:
        rule = Rule(name="aaa", required=("no aaa root",))
        result = compliance.evaluate(device, CONFIG, rules(rule))
        assert result.results[0].status is RuleStatus.PASS

    def test_missing_line_fails_and_names_it(self, device) -> None:
        rule = Rule(name="ntp", required=("ntp server 10.0.0.1",))
        result = compliance.evaluate(device, CONFIG, rules(rule))

        assert result.results[0].status is RuleStatus.FAIL
        assert result.results[0].violations[0].describe() == (
            "missing: ntp server 10.0.0.1"
        )

    def test_indented_config_line_still_matches(self, device) -> None:
        """`   ip routing` in the config satisfies `ip routing` in the rule."""
        rule = Rule(name="routing", required=("ip routing",))
        assert compliance.evaluate(device, CONFIG, rules(rule)).compliant

    def test_partial_line_does_not_match(self, device) -> None:
        """Exact matching on purpose: substring matching would let a comment
        mentioning a setting satisfy a rule requiring it."""
        rule = Rule(name="aaa", required=("no aaa",))
        assert not compliance.evaluate(device, CONFIG, rules(rule)).compliant


class TestForbidden:
    def test_absent_line_passes(self, device) -> None:
        rule = Rule(name="telnet", forbidden=("ip telnet server enable",))
        assert compliance.evaluate(device, CONFIG, rules(rule)).compliant

    def test_present_line_fails_with_line_number(self, device) -> None:
        config = CONFIG + "ip telnet server enable\n"
        rule = Rule(name="telnet", forbidden=("ip telnet server enable",))
        result = compliance.evaluate(device, config, rules(rule))

        violation = result.results[0].violations[0]
        assert violation.kind == "forbidden"
        assert violation.line_number == 16
        assert "line 16" in violation.describe()

    def test_every_occurrence_is_reported(self, device) -> None:
        config = CONFIG + "ip telnet server enable\n!\nip telnet server enable\n"
        rule = Rule(name="telnet", forbidden=("ip telnet server enable",))
        result = compliance.evaluate(device, config, rules(rule))
        assert len(result.results[0].violations) == 2


class TestRegex:
    def test_required_regex_matches_partial_line(self, device) -> None:
        import re

        rule = Rule(name="hostname", required_regex=(re.compile(r"^hostname \S+"),))
        assert compliance.evaluate(device, CONFIG, rules(rule)).compliant

    def test_required_regex_missing_reports_the_pattern(self, device) -> None:
        import re

        rule = Rule(name="ntp", required_regex=(re.compile(r"^ntp server "),))
        result = compliance.evaluate(device, CONFIG, rules(rule))
        assert result.results[0].violations[0].expected == "^ntp server "

    def test_forbidden_regex_catches_default_community(self, device) -> None:
        import re

        config = CONFIG + "snmp-server community public ro\n"
        rule = Rule(
            name="snmp",
            forbidden_regex=(re.compile(r"^snmp-server community (public|private)\b"),),
        )
        result = compliance.evaluate(device, config, rules(rule))

        assert result.results[0].status is RuleStatus.FAIL
        assert "snmp-server community public ro" in result.results[0].violations[0].found


class TestTagScoping:
    def test_non_applicable_rule_is_not_a_pass_or_a_fail(self, device) -> None:
        rule = Rule(name="leaf only", required=("nonsense",), tags=("leaf",))
        result = compliance.evaluate(device, CONFIG, rules(rule))

        assert result.results[0].status is RuleStatus.NOT_APPLICABLE
        assert result.evaluated == []
        assert result.compliant

    def test_applicable_rule_is_evaluated(self, device) -> None:
        rule = Rule(name="spine only", required=("nonsense",), tags=("spine",))
        result = compliance.evaluate(device, CONFIG, rules(rule))
        assert not result.compliant


class TestResultAggregation:
    def test_failures_sorted_by_severity(self, device) -> None:
        result = compliance.evaluate(
            device,
            CONFIG,
            rules(
                Rule(name="low one", required=("nope1",), severity=Severity.LOW),
                Rule(name="high one", required=("nope2",), severity=Severity.HIGH),
                Rule(name="medium one", required=("nope3",), severity=Severity.MEDIUM),
            ),
        )
        assert [r.rule_name for r in result.failures] == [
            "high one",
            "medium one",
            "low one",
        ]

    def test_violation_count_spans_rules(self, device) -> None:
        result = compliance.evaluate(
            device,
            CONFIG,
            rules(Rule(name="two", required=("nope1", "nope2"))),
        )
        assert result.violation_count == 2

    def test_all_passing_is_compliant(self, device) -> None:
        result = compliance.evaluate(
            device, CONFIG, rules(Rule(name="aaa", required=("no aaa root",)))
        )
        assert result.compliant


class TestAuditFromBackups:
    def test_audits_the_latest_backup(self, device, tmp_path) -> None:
        from netauditor import backup

        directory = tmp_path / device.name
        directory.mkdir()
        (directory / "20260801T000000Z.cfg").write_text("no aaa root\n", encoding="utf-8")
        (directory / "20260802T000000Z.cfg").write_text(
            "ip telnet server enable\n", encoding="utf-8"
        )

        results = compliance.audit_from_backups(
            [device], rules(Rule(name="aaa", required=("no aaa root",))), tmp_path
        )
        # The newer backup lacks the required line, so this must fail.
        assert not results[0].compliant
        assert results[0].source.endswith("20260802T000000Z.cfg")

    def test_missing_backup_is_an_error_not_a_pass(self, device, tmp_path) -> None:
        """A device with nothing to audit is unknown, and unknown must never
        read as compliant."""
        results = compliance.audit_from_backups(
            [device], rules(Rule(name="aaa", required=("no aaa root",))), tmp_path
        )
        assert results[0].error
        assert not results[0].compliant


class TestAuditFromResults:
    def test_live_config_is_audited(self, device) -> None:
        result = DeviceResult(
            device=device,
            status=DeviceStatus.SUCCESS,
            outputs={device.backup_command: CONFIG},
        )
        audited = compliance.audit_from_results(
            [result], rules(Rule(name="aaa", required=("no aaa root",)))
        )
        assert audited[0].compliant
        assert audited[0].source == "live"

    def test_unreachable_device_is_not_compliant(self, device) -> None:
        result = DeviceResult(
            device=device, status=DeviceStatus.UNREACHABLE, error="no route"
        )
        audited = compliance.audit_from_results(
            [result], rules(Rule(name="aaa", required=("no aaa root",)))
        )
        assert not audited[0].compliant
        assert audited[0].error == "no route"
