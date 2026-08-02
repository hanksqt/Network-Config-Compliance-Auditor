# Network Config Compliance Auditor

[![CI](https://github.com/hanksqt/Network-Config-Compliance-Auditor/actions/workflows/ci.yml/badge.svg)](https://github.com/hanksqt/Network-Config-Compliance-Auditor/actions/workflows/ci.yml)
[![Compliance](https://github.com/hanksqt/Network-Config-Compliance-Auditor/actions/workflows/compliance.yml/badge.svg)](https://github.com/hanksqt/Network-Config-Compliance-Auditor/actions/workflows/compliance.yml)

SSH into your network devices, back up their running configuration, and audit
each one against a golden baseline defined in YAML — so you find out a switch
drifted from standard *before* an auditor does.

> **Status: complete.** All five phases are built, tested, and verified against
> three Arista cEOS nodes running under Containerlab.

## The problem

Every network has a standard: NTP servers, AAA, SNMP communities, a login
banner, `no ip http server`. Every network also has a switch that quietly lost
one of them during a 2 a.m. change three months ago. Finding that switch by
hand means SSHing into forty boxes and reading forty configs.

This tool does that pass in seconds, tells you exactly which lines are missing
or forbidden, and exits non-zero so CI can fail the build on drift.

## Lab topology

```mermaid
graph TD
    A["auditor.py<br/>(your laptop)"] -.->|SSH :22| MGMT

    subgraph MGMT["management network 172.20.20.0/24"]
        S1["ceos-spine1<br/>172.20.20.11"]
        L1["ceos-leaf1<br/>172.20.20.12"]
        L2["ceos-leaf2<br/>172.20.20.13"]
    end

    S1 ---|eth1| L1
    S1 ---|eth2| L2
```

Three Arista cEOS containers under [Containerlab](https://containerlab.dev/).
Full setup in [lab/README.md](lab/README.md).

## Sample output

![Connectivity check against three cEOS nodes](docs/phase1-connectivity.png)

And a device that is down, to show the failure path (stop one node with
`docker stop clab-netaudit-ceos-leaf2`):

```console
│ ceos-leaf2  │ 172.20.20.13:22 │ arista_eos │ UNREACHABLE │ 10.0s │ could not open TCP session to device │
Summary: 2/3 reachable, 1 failed
```

## Features

**Built (Phase 1)**

- **YAML inventory** with a `defaults:` block, per-device overrides, and tags
  for slicing the fleet (`--tag spine`)
- **No credentials in the repo.** The inventory names a credential *profile*;
  secrets come from environment variables or a gitignored `.env`
- **Graceful per-device failure.** Unreachable, auth-failed, timed-out, and
  misconfigured devices each get their own status; one dead box never aborts
  the run
- **Concurrent collection** — devices are polled in a thread pool, so a device
  sitting on a 10-second connect timeout doesn't hold up the other thirty-nine
- **Retries with backoff** for transient failures only (retrying a wrong
  password is pointless, so it doesn't)
- **Strict inventory validation** — a typo like `devcie_type` is a startup
  error, not a confusing SSH failure ten minutes later
- **Table or JSON output**, meaningful exit codes, and 101 unit tests that run
  without touching a network

**Built (Phase 2)**

- **Timestamped backups** to `backups/<device>/<UTC>.cfg`, written atomically so
  a crash can't leave a truncated file where a config should be
- **Unchanged configs aren't rewritten**, so the directory is a change history
  rather than a cron log — volatile lines (`! Last configuration change at …`,
  `ntp clock-period`) are ignored when comparing, or every run would look like drift
- **Bad captures can't overwrite good history** — a config that's empty,
  truncated, or an error string is `REJECTED` and nothing is written
- **Optional `--git-commit`** to version config history; a failed commit never
  turns a successful backup into a failed run

**Built (Phase 3)**

- **Golden rules in YAML** — `required` / `forbidden` lines plus regex variants,
  with per-rule severity and descriptions that appear in the report
- **Rules scoped by inventory tag**, so `spine`-only rules don't need a separate file
- **Violations name the offending line and its line number**, not just "device failed"
- **Audits the latest backup by default** (reproducible, no network, CI-friendly);
  `--check --live` audits the devices' current config instead
- **Unknown never reads as compliant** — a device with no backup, or one that
  couldn't be reached, is reported as an error rather than a pass
- **Rule files are validated strictly** — a rule that defines no checks is a
  load error, because it would pass silently and look fine in the report

**Built (Phase 4)**

- **HTML and Markdown reports** via `--report path.html` / `.md` / `.json`,
  format chosen by the extension
- **Self-contained HTML** — inline CSS, no CDN, no scripts, so the report still
  works emailed, archived, or opened on a management network with no internet
- **Markdown renders natively on GitHub**, so it drops straight into a ticket,
  a PR comment, or an Actions job summary — [sample](docs/sample-report.md)
- Device-supplied text is escaped; config content reaches the report as text,
  never as markup

**Built (Phase 5)**

- **Scheduled compliance run** — weekday cron, manual trigger, and on any push
  that touches configs or rules
- **Fails the build on drift**, and publishes the Markdown report to the
  Actions job summary so the finding is visible without downloading anything
- **Needs no credentials** — auditing committed configs opens no socket, so the
  scheduled job runs with zero secrets configured

## Tech stack

| Piece | Choice | Why |
|---|---|---|
| SSH | [Netmiko](https://github.com/ktbyers/netmiko) | Handles per-vendor prompts, paging, and enable mode |
| Config | PyYAML | Inventory and golden rules live in data, not code |
| CLI | argparse | Standard library; no dependency for something this small |
| Console | [rich](https://github.com/Textualize/rich) | Colorized tables that degrade cleanly in CI logs |
| Tests | pytest | Network layer is dependency-injected, so it's testable without a lab |
| CI | GitHub Actions | Unit tests now; scheduled compliance runs in Phase 5 |

## Setup

Requires Python 3.11+.

```bash
git clone <your-repo-url> && cd net-config-auditor
python -m venv .venv
```

Activate it — `.venv\Scripts\Activate.ps1` on Windows PowerShell,
`source .venv/bin/activate` on macOS/Linux — then:

```bash
pip install -r requirements-dev.txt
```

### Credentials

Never in the repo. Copy the example and fill it in:

```bash
cp .env.example .env
```

```ini
NETAUDIT_LAB_USERNAME=admin
NETAUDIT_LAB_PASSWORD=admin
```

A device with `credentials: lab` in `inventory.yaml` resolves against
`NETAUDIT_LAB_*`, falling back per-field to the unprefixed `NETAUDIT_*`. So one
default pair covers the whole fleet, and only the devices that differ need a
profile. `.env` is gitignored; in CI, use GitHub Actions secrets instead — real
environment variables win over the file, so a stale `.env` can't shadow them.

SSH keys work too: put `key_file: ~/.ssh/netops_id_rsa` on the device (a path
isn't a secret) and drop the password.

### Lab

Follow [lab/README.md](lab/README.md) and don't move on until
`ssh admin@172.20.20.11` gets you a prompt by hand. Netmiko problems and
"the device isn't up yet" problems look identical from the CLI.

## Usage

Check what the tool parsed out of your inventory — no SSH involved:

```bash
python auditor.py --list-devices
```

Connect to every device and run its backup command:

```bash
python auditor.py --test-connection
```

Back up every device's running config:

```bash
python auditor.py --backup
```

```console
│ ceos-spine1 │ WRITTEN   │ 49 │ backups/ceos-spine1/20260802T032349Z.cfg │
│ ceos-leaf1  │ UNCHANGED │ 47 │ no change since 20260802T032349Z.cfg     │
Backups: 1 written, 1 unchanged
```

Version that history in git:

```bash
python auditor.py --backup --git-commit
```

Audit every device against the golden config:

```bash
python auditor.py --check
```

```console
│ ceos-spine1 │ NON-COMPLIANT │ 7/9 │ 3 │ Spines must route, Login banner present │
│ ceos-leaf1  │ COMPLIANT     │ 8/8 │ 0 │                                         │

ceos-spine1
  ✗ Spines must route  [high]
      - missing: ip routing
      - forbidden: no ip routing (line 38)
```

That exits non-zero, which is what lets CI fail a build on drift.

Export the report — extension picks the format:

```bash
python auditor.py --check --report reports/compliance.html
```

[docs/sample-report.md](docs/sample-report.md) is real output from a run where
the lab had drifted — a missing login banner on all three nodes, `no ip routing`
on a spine, and a default SNMP community. The committed configs are compliant
now, which is why the badge is green; re-injecting drift takes one command.

Slice the fleet, tune concurrency, get machine-readable output:

```bash
python auditor.py --test-connection --tag leaf --workers 16 --json
```

Debug a single device with full SSH logging:

```bash
python auditor.py --test-connection --device ceos-leaf1 -vv
```

`--help` lists everything. Useful flags: `--device/-d` and `--tag` to filter,
`--command/-c` to run something other than the backup command, `--retries`,
`--json`, `--show-output`, `--no-color`, `-v`/`-vv`.

### Exit codes

| Code | Meaning |
|---|---|
| 0 | Everything succeeded |
| 1 | Run completed, but one or more devices failed |
| 2 | Run couldn't start — bad inventory, missing credentials, bad arguments |
| 130 | Interrupted |

The 1-vs-2 split is deliberate: CI should treat "a switch is unreachable"
differently from "your inventory file is broken."

## Running this against real devices in CI

The scheduled [Compliance workflow](.github/workflows/compliance.yml) audits the
configs committed under `backups/`. That is a real check — it catches drift the
moment a changed config lands — and it needs no network access and no secrets.

It does **not** SSH to devices, and no GitHub-hosted runner can: your devices
sit on a private management network that `ubuntu-latest` has no route to. A
workflow claiming otherwise would be theatre.

To audit live devices on a schedule, you need a runner that can reach them:

1. Register a [self-hosted runner](https://docs.github.com/en/actions/hosting-your-own-runners)
   on the management network
2. Add `NETAUDIT_LAB_USERNAME` and `NETAUDIT_LAB_PASSWORD` as repository secrets
3. In `compliance.yml`, change the `live-audit` job's `if: false` to
   `if: github.event_name == 'schedule'`

That job runs `--backup` and *then* `--check --live`, in that order and on
purpose: auditing whatever was committed last week would report green while a
switch is actively misconfigured.

## Tests

```bash
pytest
```

101 tests, no network required — the Netmiko connection factory is injected, so
the error taxonomy, retry logic, concurrency, and CLI exit codes are all tested
against a fake device.

## Project layout

```
auditor.py              # entry point
netauditor/
  cli.py                # argparse, exit codes
  inventory.py          # YAML parsing + validation
  credentials.py        # env-var resolution
  connect.py            # Netmiko wrapper, error classification, retries
  runner.py             # thread pool across devices
  backup.py             # timestamped writes, change detection, sanity checks
  gitstore.py           # optional git commit of new backups
  golden.py             # golden.yaml parsing + strict validation
  compliance.py         # rule evaluation, violation reporting
  render.py             # HTML + Markdown report generation
  report.py             # console tables + JSON
  models.py             # Device, DeviceResult, DeviceStatus
inventory.yaml          # devices (no secrets)
golden.yaml             # what "compliant" means
lab/                    # Containerlab topology + Phase 0 instructions
tests/
```

## Design decisions

**Why Netmiko over raw Paramiko.** Paramiko gives you a shell channel; it does
not know that IOS pages output with `--More--`, that EOS needs `terminal length
0`, or how to get into enable mode. Netmiko is one dependency that removes an
entire category of per-vendor bugs, and its platform names (`arista_eos`,
`cisco_xe`) become the `device_type` field in the inventory — the schema is
already multivendor even though the lab is currently all EOS.

**Why golden rules live in YAML, not Python.** A compliance tool whose rules are
hardcoded is a script for exactly one network. Keeping the inventory and (in
Phase 3) the rule set in data means the same binary audits lab and production
with different files, and a network engineer who doesn't write Python can still
change what "compliant" means.

**Why credentials are profiles, not values.** `inventory.yaml` is committed, so
it can only ever name a profile. The mapping from `credentials: lab` to
`NETAUDIT_LAB_PASSWORD` is mechanical, which means adding a device with
different credentials needs no code change and no secret in git. Secrets are
also marked `repr=False` on the dataclass, so they can't leak into a traceback
or a debug log.

**Why a successful SSH session isn't a successful collection.** The first run
against real cEOS returned `OK` for all three devices — and the "config" it
collected was `% Invalid input (privileged mode required)`. Netmiko had done its
job perfectly: it connected, sent the command, and returned the answer. The
device just happened to answer with a refusal. `output_problem()` now inspects
what came back, so a rejection is `COMMAND_FAILED` rather than a one-line file
that Phase 3 would cheerfully audit against golden rules. Every layer that only
checks *transport* success has this hole in it.

**Why rule matching is exact, not substring.** `required: ["ip routing"]`
matches a config line of `ip routing` (or `   ip routing` — indentation and
repeated spaces are normalized away) but *not* `ip routing ipv6`. Substring
matching would be friendlier right up until `forbidden: ["ip http server"]` is
satisfied by a comment mentioning it, or a banner quoting it. In a compliance
tool the expensive failure is a rule that silently passes, so matching is
exact, and `required_regex` / `forbidden_regex` handle everything else
explicitly. A rule either matches what you meant or visibly does not.

**Why a rule with no checks is a load error.** `golden.py` rejects a rule that
defines none of `required`/`forbidden`/`required_regex`/`forbidden_regex`, and
rejects unknown keys like `forbiden`. Both would otherwise produce a rule that
always passes — which is indistinguishable, in the report, from a rule that
genuinely passed. Regexes are compiled at load for the same reason: fail at
startup, not halfway through an audit.

**Why an offline audit needs no credentials.** `--check` against committed
backups opens no socket, so `load_inventory(require_credentials=False)` leaves
`Device.credentials` as `None` and any attempt to connect raises instead of
proceeding. This came out of writing the CI workflow: the first draft had to
inject fake `NETAUDIT_*` values so an *offline* operation would start, which is
a design smell wearing a workaround as a disguise. Now the scheduled job runs
with zero secrets configured, which is also the correct security posture — a
job that cannot reach devices should not be handed device credentials.

**Why the HTML report has no CDN link in it.** Everything is inlined — CSS in a
`<style>` block, no scripts, no external fonts. A compliance report gets
emailed to an auditor, attached to a ticket, archived for a year, or opened
from a jump host on a management network with no internet route. Any of those
break a report that fetches a stylesheet at render time, and it breaks *later*,
when nobody is around to notice. Same reason config text is HTML-escaped:
device output ends up in the report, so it has to arrive as text rather than as
markup.

**Why unreachable means non-compliant.** A device with no backup to audit, or
one that couldn't be collected, is reported as an error and counted against the
run. It would be easy to skip those and report "3/3 compliant" from two
devices, which is how a compliance report becomes a lie.

**Why the lab's backups are committed, and why yours might not be.** `backups/`
contains real captures from the Containerlab topology, including
`username admin … secret sha512 $6$…`. That is deliberate: the hash is of
`admin`, containerlab's documented default, salted, on a disposable container
on an RFC1918 network that is destroyed with `containerlab destroy`. Nothing
there is a secret, and committing it is what makes config history visible
rather than theoretical — `git log backups/ceos-leaf1/` is the feature working.

A production fleet is the opposite case. Running-configs carry credential
hashes, SNMP communities, and pre-shared keys, and a real backup repo belongs
somewhere private with restricted access — or `backups/` goes in `.gitignore`
and the configs go to storage with an access policy. The tool doesn't care
which; `--git-commit` is opt-in for exactly this reason.

**Why one dead device can't fail the run.** `connect.collect()` never raises —
it classifies the exception and returns a result. That is what makes the
difference between a tool that audits 39 of 40 switches and tells you about the
40th, and a tool that crashes on device 12 and audits nothing. The classifier
is a pure function, which is why it can be unit tested against nine different
failure modes without a lab.

**Why retries are selective.** Only `UNREACHABLE` and `TIMEOUT` are retried. An
authentication failure or an unsupported `device_type` is deterministic —
retrying it four more times just makes a failing run slower.

**Why a thread pool.** SSH collection is almost entirely blocking I/O. Forty
sequential devices, each with a 10-second connect timeout, is a worst case of
nearly seven minutes; eight workers turns that into under a minute. asyncio
would work too, but Netmiko's async support is immature and threads are the
honest fit for a blocking library.

## Roadmap

- [x] **Phase 0** — Containerlab topology, three cEOS nodes reachable over SSH
- [x] **Phase 1** — inventory, credentials, connectivity, error handling, CLI
- [x] **Phase 2** — back up configs to `backups/<hostname>/<timestamp>.cfg`, optional git commit
- [x] **Phase 3** — compliance engine: `required` / `forbidden` / regex rules in YAML
- [x] **Phase 4** — HTML and Markdown reports alongside the console output
- [x] **Phase 5** — scheduled GitHub Actions compliance run that fails on drift

v2 ideas: a diff view between two backups, multivendor rule sets, and a Nornir
rewrite.
