"""Golden config parsing and validation.

The dangerous failure for a compliance tool is a rule that silently passes, so
most of this file is about rejecting rule files that would do that.
"""

from __future__ import annotations

import pytest
import yaml

from netauditor import golden
from netauditor.golden import GoldenError
from netauditor.models import Severity


@pytest.fixture
def write_golden(tmp_path):
    def _write(data: dict, name: str = "golden.yaml"):
        path = tmp_path / name
        path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
        return path

    return _write


BASIC = {
    "rules": [
        {"name": "AAA root disabled", "required": ["no aaa root"]},
        {"name": "No telnet", "forbidden": ["ip telnet server enable"]},
    ]
}


class TestLoading:
    def test_loads_rules(self, write_golden) -> None:
        config = golden.load_golden(write_golden(BASIC))
        assert [r.name for r in config.rules] == ["AAA root disabled", "No telnet"]

    def test_single_string_is_accepted_as_a_list(self, write_golden) -> None:
        data = {"rules": [{"name": "r", "required": "no aaa root"}]}
        assert golden.load_golden(write_golden(data)).rules[0].required == (
            "no aaa root",
        )

    def test_severity_defaults_to_high(self, write_golden) -> None:
        assert golden.load_golden(write_golden(BASIC)).rules[0].severity is Severity.HIGH

    def test_defaults_block_applies(self, write_golden) -> None:
        data = {"defaults": {"severity": "low"}, "rules": BASIC["rules"]}
        assert golden.load_golden(write_golden(data)).rules[0].severity is Severity.LOW

    def test_rule_overrides_default_severity(self, write_golden) -> None:
        data = {
            "defaults": {"severity": "low"},
            "rules": [{"name": "r", "required": ["x"], "severity": "high"}],
        }
        assert golden.load_golden(write_golden(data)).rules[0].severity is Severity.HIGH

    def test_regex_is_compiled(self, write_golden) -> None:
        data = {"rules": [{"name": "r", "required_regex": [r"^hostname \S+"]}]}
        rule = golden.load_golden(write_golden(data)).rules[0]
        assert rule.required_regex[0].search("hostname ceos-spine1")


class TestValidation:
    def test_missing_file(self, tmp_path) -> None:
        with pytest.raises(GoldenError, match="not found"):
            golden.load_golden(tmp_path / "nope.yaml")

    def test_empty_file(self, tmp_path) -> None:
        path = tmp_path / "golden.yaml"
        path.write_text("", encoding="utf-8")
        with pytest.raises(GoldenError, match="empty"):
            golden.load_golden(path)

    def test_no_rules(self, write_golden) -> None:
        with pytest.raises(GoldenError, match="no rules"):
            golden.load_golden(write_golden({"rules": []}))

    def test_rule_with_no_checks_is_rejected(self, write_golden) -> None:
        """A rule that checks nothing always passes -- the worst possible bug
        in a compliance tool, and completely invisible in a report."""
        data = {"rules": [{"name": "does nothing", "description": "oops"}]}
        with pytest.raises(GoldenError, match="defines no checks"):
            golden.load_golden(write_golden(data))

    def test_typo_in_key_is_rejected(self, write_golden) -> None:
        """`forbiden` would silently make a security rule vacuous."""
        data = {"rules": [{"name": "r", "forbiden": ["ip telnet server enable"]}]}
        with pytest.raises(GoldenError, match="forbiden"):
            golden.load_golden(write_golden(data))

    def test_missing_name(self, write_golden) -> None:
        with pytest.raises(GoldenError, match="'name' is required"):
            golden.load_golden(write_golden({"rules": [{"required": ["x"]}]}))

    def test_duplicate_name(self, write_golden) -> None:
        data = {"rules": [{"name": "r", "required": ["a"]}, {"name": "r", "required": ["b"]}]}
        with pytest.raises(GoldenError, match="duplicate"):
            golden.load_golden(write_golden(data))

    def test_invalid_severity(self, write_golden) -> None:
        data = {"rules": [{"name": "r", "required": ["x"], "severity": "critical"}]}
        with pytest.raises(GoldenError, match="severity must be one of"):
            golden.load_golden(write_golden(data))

    def test_invalid_regex_is_caught_at_load(self, write_golden) -> None:
        """Better to fail at startup than halfway through an audit."""
        data = {"rules": [{"name": "r", "required_regex": ["[unclosed"]}]}
        with pytest.raises(GoldenError, match="invalid regex"):
            golden.load_golden(write_golden(data))

    def test_unknown_top_level_key(self, write_golden) -> None:
        with pytest.raises(GoldenError, match="devices"):
            golden.load_golden(write_golden({"rules": BASIC["rules"], "devices": []}))


class TestTagScoping:
    def _rules(self, write_golden, tags):
        data = {"rules": [{"name": "r", "required": ["ip routing"], "tags": tags}]}
        return golden.load_golden(write_golden(data)).rules[0]

    def test_untagged_rule_applies_everywhere(self, write_golden, device) -> None:
        data = {"rules": [{"name": "r", "required": ["x"]}]}
        rule = golden.load_golden(write_golden(data)).rules[0]
        assert rule.applies_to(device)

    def test_matching_tag_applies(self, write_golden, device) -> None:
        assert self._rules(write_golden, ["spine"]).applies_to(device)

    def test_non_matching_tag_does_not_apply(self, write_golden, device) -> None:
        assert not self._rules(write_golden, ["leaf"]).applies_to(device)

    def test_tag_match_is_case_insensitive(self, write_golden, device) -> None:
        assert self._rules(write_golden, ["SPINE"]).applies_to(device)

    def test_for_device_filters(self, write_golden, device) -> None:
        data = {
            "rules": [
                {"name": "everywhere", "required": ["a"]},
                {"name": "spines", "required": ["b"], "tags": ["spine"]},
                {"name": "leaves", "required": ["c"], "tags": ["leaf"]},
            ]
        }
        config = golden.load_golden(write_golden(data))
        assert [r.name for r in config.for_device(device)] == ["everywhere", "spines"]


class TestShippedGoldenFile:
    def test_repo_golden_yaml_is_valid(self) -> None:
        """The file in the repo must parse -- it is what CI audits against."""
        from pathlib import Path

        config = golden.load_golden(Path(__file__).parent.parent / "golden.yaml")
        assert len(config.rules) >= 5
        assert all(
            r.required or r.forbidden or r.required_regex or r.forbidden_regex
            for r in config.rules
        )
