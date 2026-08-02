"""Exceptions raised by the auditor itself (as opposed to by Netmiko)."""

from __future__ import annotations


class AuditorError(Exception):
    """Base class for all auditor errors."""


class InventoryError(AuditorError):
    """inventory.yaml is missing, malformed, or fails validation."""


class CredentialError(AuditorError):
    """Required credentials were not found in the environment."""
