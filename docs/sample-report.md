# Network Compliance Report

_Generated 2026-08-02 03:46:27 UTC_

**0/3 devices compliant** — 6 violation(s)

| Severity | Violations |
| --- | ---: |
| high | 3 |
| low | 3 |

## Devices

| Device | Host | Result | Rules passed | Violations |
| --- | --- | --- | ---: | ---: |
| `ceos-spine1` | `172.20.20.11` | **NON-COMPLIANT** | 7/9 | 3 |
| `ceos-leaf1` | `172.20.20.12` | **NON-COMPLIANT** | 6/8 | 2 |
| `ceos-leaf2` | `172.20.20.13` | **NON-COMPLIANT** | 7/8 | 1 |

## Findings

### ceos-spine1

**Spines must route** — `high`

> A spine with ip routing disabled is a very expensive switch. Leaves in this topology are intentionally L2, so the rule is scoped by tag.

- missing: ip routing
- forbidden: no ip routing (line 38)

**Login banner present** — `low`

> A login banner is required for legal notice before access. Checked as a regex because the banner text itself varies.

- missing: ^banner motd

_Source: `live`_

### ceos-leaf1

**No default SNMP communities** — `high`

> public/private are guessable and grant read (or write) access to the whole device. This is the single most common real audit finding.

- forbidden: snmp-server community public ro (line 19)

**Login banner present** — `low`

> A login banner is required for legal notice before access. Checked as a regex because the banner text itself varies.

- missing: ^banner motd

_Source: `live`_

### ceos-leaf2

**Login banner present** — `low`

> A login banner is required for legal notice before access. Checked as a regex because the banner text itself varies.

- missing: ^banner motd

_Source: `live`_
