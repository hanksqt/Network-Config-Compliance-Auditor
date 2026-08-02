"""Credential resolution: the part that must never leak a secret into git."""

from __future__ import annotations

import pytest

from netauditor import credentials as creds
from netauditor.errors import CredentialError
from netauditor.models import Credentials


class TestEnvPrefix:
    @pytest.mark.parametrize(
        ("profile", "expected"),
        [
            ("lab", "NETAUDIT_LAB"),
            ("Lab", "NETAUDIT_LAB"),
            ("lab-edge", "NETAUDIT_LAB_EDGE"),
            ("campus.core", "NETAUDIT_CAMPUS_CORE"),
            ("dc 1", "NETAUDIT_DC_1"),
        ],
    )
    def test_profile_slugified(self, profile: str, expected: str) -> None:
        assert creds.profile_to_env_prefix(profile) == expected

    def test_empty_profile_rejected(self) -> None:
        with pytest.raises(CredentialError):
            creds.profile_to_env_prefix("---")


class TestResolve:
    def test_profile_wins_over_default(self) -> None:
        env = {
            "NETAUDIT_USERNAME": "default-user",
            "NETAUDIT_PASSWORD": "default-pw",
            "NETAUDIT_LAB_USERNAME": "lab-user",
            "NETAUDIT_LAB_PASSWORD": "lab-pw",
        }
        result = creds.resolve("r1", "lab", env=env)
        assert result.username == "lab-user"
        assert result.password == "lab-pw"

    def test_falls_back_per_field(self) -> None:
        """A profile that sets only a password still inherits the username."""
        env = {
            "NETAUDIT_USERNAME": "default-user",
            "NETAUDIT_PASSWORD": "default-pw",
            "NETAUDIT_LAB_PASSWORD": "lab-pw",
        }
        result = creds.resolve("r1", "lab", env=env)
        assert result.username == "default-user"
        assert result.password == "lab-pw"

    def test_no_profile_uses_defaults(self) -> None:
        env = {"NETAUDIT_USERNAME": "u", "NETAUDIT_PASSWORD": "p"}
        assert creds.resolve("r1", None, env=env).username == "u"

    def test_enable_secret_optional(self) -> None:
        env = {"NETAUDIT_USERNAME": "u", "NETAUDIT_PASSWORD": "p"}
        assert creds.resolve("r1", None, env=env).enable_secret is None

        env["NETAUDIT_ENABLE"] = "s3cret"
        assert creds.resolve("r1", None, env=env).enable_secret == "s3cret"

    def test_empty_string_treated_as_unset(self) -> None:
        env = {
            "NETAUDIT_USERNAME": "u",
            "NETAUDIT_PASSWORD": "p",
            "NETAUDIT_LAB_USERNAME": "",
        }
        assert creds.resolve("r1", "lab", env=env).username == "u"

    def test_missing_username_names_the_variables(self) -> None:
        with pytest.raises(CredentialError) as exc:
            creds.resolve("r1", "lab", env={})
        message = str(exc.value)
        assert "NETAUDIT_LAB_USERNAME" in message
        assert "NETAUDIT_USERNAME" in message
        assert "r1" in message

    def test_missing_password_and_key_rejected(self) -> None:
        with pytest.raises(CredentialError, match="no password or key_file"):
            creds.resolve("r1", "lab", env={"NETAUDIT_USERNAME": "u"})

    def test_key_file_replaces_password(self, tmp_path) -> None:
        key = tmp_path / "id_rsa"
        key.write_text("not-a-real-key", encoding="utf-8")

        result = creds.resolve(
            "r1", None, env={"NETAUDIT_USERNAME": "u"}, key_file=str(key)
        )
        assert result.uses_key
        assert result.password is None

    def test_key_file_from_env(self, tmp_path) -> None:
        key = tmp_path / "id_rsa"
        key.write_text("not-a-real-key", encoding="utf-8")

        result = creds.resolve(
            "r1",
            None,
            env={"NETAUDIT_USERNAME": "u", "NETAUDIT_KEY_FILE": str(key)},
        )
        assert result.key_file == str(key)

    def test_missing_key_file_rejected(self, tmp_path) -> None:
        with pytest.raises(CredentialError, match="does not exist"):
            creds.resolve(
                "r1",
                None,
                env={"NETAUDIT_USERNAME": "u"},
                key_file=str(tmp_path / "nope"),
            )


class TestSecretHygiene:
    """Secrets must not appear in reprs, which end up in logs and tracebacks."""

    def test_password_not_in_repr(self) -> None:
        text = repr(Credentials(username="admin", password="hunter2", enable_secret="en4ble"))
        assert "hunter2" not in text
        assert "en4ble" not in text
        assert "admin" in text

    def test_credentials_not_in_device_repr(self, device) -> None:
        text = repr(device)
        assert "Credentials" not in text
        assert "admin" not in text
        assert "ceos-spine1" in text  # the useful parts are still there
