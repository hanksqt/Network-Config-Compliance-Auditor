"""Render compliance results as standalone HTML or Markdown.

Two output formats because they are read in different places: Markdown pastes
into a ticket, a PR comment, or a GitHub Actions job summary; HTML is what you
publish or attach when someone wants to *look* at it.

The HTML is deliberately a single self-contained file with inline CSS. A report
that depends on a CDN is useless the moment it is emailed, archived, or opened
on a management network with no internet access.
"""

from __future__ import annotations

import html
from datetime import datetime, timezone
from typing import Sequence

from .models import ComplianceResult, RuleResult, Severity

SEVERITY_ORDER = (Severity.HIGH, Severity.MEDIUM, Severity.LOW)


def _timestamp(when: datetime | None = None) -> str:
    return (when or datetime.now(timezone.utc)).strftime("%Y-%m-%d %H:%M:%S UTC")


def _counts(results: Sequence[ComplianceResult]) -> dict[str, int]:
    return {
        "total": len(results),
        "compliant": sum(1 for r in results if r.compliant),
        "violations": sum(r.violation_count for r in results),
        "errors": sum(1 for r in results if r.error),
    }


def _severity_totals(results: Sequence[ComplianceResult]) -> dict[Severity, int]:
    totals = {severity: 0 for severity in SEVERITY_ORDER}
    for result in results:
        for rule in result.failures:
            totals[rule.severity] += len(rule.violations)
    return totals


def _status_of(result: ComplianceResult) -> tuple[str, str]:
    """(label, css/emphasis class) for one device."""
    if result.error:
        return "ERROR", "error"
    if result.compliant:
        return "COMPLIANT", "pass"
    return "NON-COMPLIANT", "fail"


# --------------------------------------------------------------------------
# Markdown
# --------------------------------------------------------------------------


def to_markdown(
    results: Sequence[ComplianceResult],
    *,
    when: datetime | None = None,
    title: str = "Network Compliance Report",
) -> str:
    counts = _counts(results)
    severities = _severity_totals(results)

    lines: list[str] = [
        f"# {title}",
        "",
        f"_Generated {_timestamp(when)}_",
        "",
        f"**{counts['compliant']}/{counts['total']} devices compliant**"
        + (f" — {counts['violations']} violation(s)" if counts["violations"] else ""),
        "",
    ]

    if any(severities.values()):
        lines += [
            "| Severity | Violations |",
            "| --- | ---: |",
        ]
        lines += [
            f"| {severity.value} | {count} |"
            for severity, count in severities.items()
            if count
        ]
        lines.append("")

    lines += [
        "## Devices",
        "",
        "| Device | Host | Result | Rules passed | Violations |",
        "| --- | --- | --- | ---: | ---: |",
    ]
    for result in results:
        label, _ = _status_of(result)
        passed = len(result.evaluated) - len(result.failures)
        lines.append(
            f"| `{result.device.name}` | `{result.device.host}` | **{label}** | "
            f"{passed}/{len(result.evaluated)} | {result.violation_count} |"
        )
    lines.append("")

    offenders = [r for r in results if r.failures or r.error]
    if not offenders:
        lines += ["All devices are compliant.", ""]
        return "\n".join(lines)

    lines += ["## Findings", ""]
    for result in offenders:
        lines.append(f"### {result.device.name}")
        lines.append("")
        if result.error:
            lines += [f"> Could not be checked: {result.error}", ""]
            continue
        for rule in result.failures:
            lines.append(f"**{rule.rule_name}** — `{rule.severity.value}`")
            lines.append("")
            if rule.description:
                lines += [f"> {rule.description.strip()}", ""]
            for violation in rule.violations:
                lines.append(f"- {violation.describe()}")
            lines.append("")
        if result.source:
            lines += [f"_Source: `{result.source}`_", ""]

    return "\n".join(lines)


# --------------------------------------------------------------------------
# HTML
# --------------------------------------------------------------------------

_CSS = """
:root { color-scheme: light dark; }
* { box-sizing: border-box; }
body {
  margin: 0; padding: 2rem 1.25rem;
  font: 15px/1.55 ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif;
  background: #f6f7f9; color: #1a1d21;
}
.wrap { max-width: 60rem; margin: 0 auto; }
h1 { font-size: 1.6rem; margin: 0 0 .25rem; }
h2 { font-size: 1.15rem; margin: 2rem 0 .75rem; }
.meta { color: #6b7280; font-size: .875rem; margin-bottom: 1.5rem; }
.cards { display: flex; flex-wrap: wrap; gap: .75rem; margin-bottom: 1.5rem; }
.card {
  flex: 1 1 8rem; background: #fff; border: 1px solid #e3e6ea;
  border-radius: .5rem; padding: .85rem 1rem;
}
.card .n { font-size: 1.6rem; font-weight: 650; }
.card .l { color: #6b7280; font-size: .8rem; text-transform: uppercase;
           letter-spacing: .04em; }
.table-scroll { overflow-x: auto; }
table { border-collapse: collapse; width: 100%; background: #fff;
        border: 1px solid #e3e6ea; border-radius: .5rem; }
th, td { text-align: left; padding: .55rem .8rem;
         border-bottom: 1px solid #eef0f3; }
th { font-size: .75rem; text-transform: uppercase; letter-spacing: .04em;
     color: #6b7280; }
tr:last-child td { border-bottom: 0; }
td.num, th.num { text-align: right; }
code, .mono { font-family: ui-monospace, "Cascadia Code", Consolas, monospace;
              font-size: .875em; }
.badge { display: inline-block; padding: .1rem .5rem; border-radius: 999px;
         font-size: .75rem; font-weight: 650; }
.pass  { background: #dcfce7; color: #166534; }
.fail  { background: #fee2e2; color: #991b1b; }
.error { background: #ede9fe; color: #5b21b6; }
.high   { background: #fee2e2; color: #991b1b; }
.medium { background: #fef3c7; color: #92400e; }
.low    { background: #e0f2fe; color: #075985; }
.device { background: #fff; border: 1px solid #e3e6ea; border-radius: .5rem;
          padding: 1rem 1.15rem; margin-bottom: 1rem; }
.device h3 { margin: 0 0 .75rem; font-size: 1rem; }
.rule { border-left: 3px solid #e3e6ea; padding: .1rem 0 .1rem .8rem;
        margin: .9rem 0; }
.rule .name { font-weight: 650; }
.rule .desc { color: #6b7280; font-size: .875rem; margin: .3rem 0 .5rem; }
ul.violations { margin: .3rem 0 0; padding-left: 1.1rem; }
ul.violations li { margin: .15rem 0; }
.empty { background: #dcfce7; border: 1px solid #86efac; color: #166534;
         padding: 1rem; border-radius: .5rem; }
@media (prefers-color-scheme: dark) {
  body { background: #0f1115; color: #e6e8eb; }
  .card, table, .device { background: #171a21; border-color: #262b34; }
  th, td { border-color: #222831; }
  .card .l, .meta, .rule .desc { color: #9aa4b2; }
  .rule { border-left-color: #2c333d; }
  .pass  { background: #14532d; color: #bbf7d0; }
  .fail  { background: #7f1d1d; color: #fecaca; }
  .error { background: #4c1d95; color: #ddd6fe; }
  .high   { background: #7f1d1d; color: #fecaca; }
  .medium { background: #78350f; color: #fde68a; }
  .low    { background: #0c4a6e; color: #bae6fd; }
  .empty { background: #14532d; border-color: #166534; color: #bbf7d0; }
}
"""


def _e(text: object) -> str:
    return html.escape(str(text), quote=True)


def _rule_html(rule: RuleResult) -> str:
    parts = [
        '<div class="rule">',
        f'<div><span class="name">{_e(rule.rule_name)}</span> '
        f'<span class="badge {rule.severity.value}">{rule.severity.value}</span></div>',
    ]
    if rule.description:
        parts.append(f'<div class="desc">{_e(rule.description.strip())}</div>')
    parts.append('<ul class="violations">')
    parts += [
        f"<li><code>{_e(v.describe())}</code></li>" for v in rule.violations
    ]
    parts += ["</ul>", "</div>"]
    return "".join(parts)


def to_html(
    results: Sequence[ComplianceResult],
    *,
    when: datetime | None = None,
    title: str = "Network Compliance Report",
) -> str:
    counts = _counts(results)
    severities = _severity_totals(results)

    rows = []
    for result in results:
        label, css = _status_of(result)
        passed = len(result.evaluated) - len(result.failures)
        rows.append(
            "<tr>"
            f"<td><code>{_e(result.device.name)}</code></td>"
            f"<td><code>{_e(result.device.host)}</code></td>"
            f'<td><span class="badge {css}">{label}</span></td>'
            f'<td class="num">{passed}/{len(result.evaluated)}</td>'
            f'<td class="num">{result.violation_count}</td>'
            "</tr>"
        )

    cards = [
        ("Devices", counts["total"]),
        ("Compliant", counts["compliant"]),
        ("Violations", counts["violations"]),
    ]
    cards += [
        (severity.value.title(), severities[severity])
        for severity in SEVERITY_ORDER
        if severities[severity]
    ]
    if counts["errors"]:
        cards.append(("Unchecked", counts["errors"]))

    card_html = "".join(
        f'<div class="card"><div class="n">{value}</div>'
        f'<div class="l">{_e(label)}</div></div>'
        for label, value in cards
    )

    offenders = [r for r in results if r.failures or r.error]
    if offenders:
        findings = []
        for result in offenders:
            block = [f'<div class="device"><h3>{_e(result.device.name)}</h3>']
            if result.error:
                block.append(
                    f'<p class="desc">Could not be checked: {_e(result.error)}</p>'
                )
            else:
                block += [_rule_html(rule) for rule in result.failures]
                if result.source:
                    block.append(
                        f'<p class="desc mono">Source: {_e(result.source)}</p>'
                    )
            block.append("</div>")
            findings.append("".join(block))
        findings_html = "<h2>Findings</h2>" + "".join(findings)
    else:
        findings_html = '<div class="empty">All devices are compliant.</div>'

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_e(title)}</title>
<style>{_CSS}</style>
</head>
<body>
<div class="wrap">
<h1>{_e(title)}</h1>
<div class="meta">Generated {_timestamp(when)}</div>
<div class="cards">{card_html}</div>
<h2>Devices</h2>
<div class="table-scroll">
<table>
<thead><tr><th>Device</th><th>Host</th><th>Result</th>
<th class="num">Rules passed</th><th class="num">Violations</th></tr></thead>
<tbody>{"".join(rows)}</tbody>
</table>
</div>
{findings_html}
</div>
</body>
</html>
"""
