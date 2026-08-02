"""Optionally commit new backups, so config history is versioned.

Shelling out to ``git`` rather than adding GitPython: this needs three
commands, and a dependency that bundles its own git semantics is a poor trade
for that. Every failure here is non-fatal — a backup that was written to disk
is still a successful backup even if the commit fails.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import Sequence

log = logging.getLogger(__name__)

GIT_TIMEOUT = 30


class GitError(Exception):
    """A git command failed."""


def _git(args: Sequence[str], cwd: Path) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=GIT_TIMEOUT,
        check=False,
    )
    if result.returncode != 0:
        message = (result.stderr or result.stdout).strip().splitlines()
        raise GitError(message[0] if message else f"git {args[0]} failed")
    return result.stdout.strip()


def is_repo(path: Path) -> bool:
    try:
        return _git(["rev-parse", "--is-inside-work-tree"], path) == "true"
    except (GitError, OSError, subprocess.SubprocessError):
        return False


def commit_paths(
    paths: Sequence[Path],
    repo_root: Path,
    message: str,
) -> str | None:
    """Stage ``paths`` and commit them. Returns the short SHA, or ``None``.

    ``None`` means there was nothing to commit — not a failure. Raises
    :class:`GitError` only when git itself refused to do the work.
    """
    if not paths:
        return None

    if not is_repo(repo_root):
        raise GitError(f"{repo_root} is not a git repository")

    _git(["add", "--", *(str(p) for p in paths)], repo_root)

    # --cached because the paths are staged but not yet committed; an empty
    # diff means the files were already tracked and unchanged.
    try:
        _git(["diff", "--cached", "--quiet"], repo_root)
    except GitError:
        pass  # non-zero exit means there ARE staged changes, which is what we want
    else:
        log.debug("nothing staged to commit")
        return None

    _git(["commit", "-m", message], repo_root)
    return _git(["rev-parse", "--short", "HEAD"], repo_root)


def default_message(written: int, devices: Sequence[str]) -> str:
    """Commit subject that says what changed without listing forty hostnames."""
    if written == 1:
        return f"Back up config for {devices[0]}"
    shown = ", ".join(devices[:3])
    if len(devices) > 3:
        shown += f", +{len(devices) - 3} more"
    return f"Back up configs for {written} devices ({shown})"
