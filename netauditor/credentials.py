"""Credential resolution from the environment.

Design decision: credentials never appear in inventory.yaml. The inventory is
committed to git, so it may only name a *credential profile*; the secrets
themselves come from environment variables (a gitignored ``.env`` locally,
GitHub Actions secrets in CI).

A device with ``credentials: lab`` resolves against::

    NETAUDIT_LAB_USERNAME
    NETAUDIT_LAB_PASSWORD
    NETAUDIT_LAB_ENABLE
    NETAUDIT_LAB_KEY_FILE

falling back to the unprefixed ``NETAUDIT_USERNAME`` / ``NETAUDIT_PASSWORD`` /
``NETAUDIT_ENABLE`` / ``NETAUDIT_KEY_FILE`` for anything the profile omits.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Mapping

from .errors import CredentialError
from .models import Credentials

ENV_PREFIX = "NETAUDIT"

_NON_ALNUM = re.compile(r"[^A-Z0-9]+")


def profile_to_env_prefix(profile: str) -> str:
    """``lab-edge`` -> ``NETAUDIT_LAB_EDGE``."""
    slug = _NON_ALNUM.sub("_", profile.upper()).strip("_")
    if not slug:
        raise CredentialError(f"credential profile {profile!r} has no usable characters")
    return f"{ENV_PREFIX}_{slug}"


def _lookup(env: Mapping[str, str], prefixes: list[str], suffix: str) -> str | None:
    """First non-empty value of ``<prefix>_<suffix>`` across prefixes, in order."""
    for prefix in prefixes:
        value = env.get(f"{prefix}_{suffix}")
        if value:
            return value
    return None


def resolve(
    device_name: str,
    profile: str | None = None,
    *,
    env: Mapping[str, str] | None = None,
    key_file: str | None = None,
) -> Credentials:
    """Build :class:`Credentials` for one device.

    ``key_file`` is the value from inventory.yaml, which wins over the
    environment (the inventory is the natural place to record *which* key a
    device uses, since a path is not a secret).

    Raises:
        CredentialError: if no username is set, or if neither a password nor a
            key file is available.
    """
    env = os.environ if env is None else env

    prefixes = [ENV_PREFIX]
    if profile:
        prefixes.insert(0, profile_to_env_prefix(profile))

    username = _lookup(env, prefixes, "USERNAME")
    password = _lookup(env, prefixes, "PASSWORD")
    enable_secret = _lookup(env, prefixes, "ENABLE")
    resolved_key = key_file or _lookup(env, prefixes, "KEY_FILE")

    tried = ", ".join(f"{p}_USERNAME" for p in prefixes)
    if not username:
        raise CredentialError(
            f"no username for device {device_name!r}: set one of {tried}. "
            f"See .env.example."
        )

    if resolved_key:
        expanded = Path(resolved_key).expanduser()
        if not expanded.is_file():
            raise CredentialError(
                f"key_file for device {device_name!r} does not exist: {expanded}"
            )
        resolved_key = str(expanded)
    elif not password:
        tried_pw = ", ".join(f"{p}_PASSWORD" for p in prefixes)
        raise CredentialError(
            f"no password or key_file for device {device_name!r}: "
            f"set one of {tried_pw}, or add key_file: to the inventory entry."
        )

    return Credentials(
        username=username,
        password=password,
        enable_secret=enable_secret,
        key_file=resolved_key,
    )
