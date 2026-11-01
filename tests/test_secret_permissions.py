"""Regression tests for systemd credential file permissions.

Source tokens remain root-owned 0600. DynamicUser credentials may appear
0440 due to systemd ACLs; only that trusted LoadCredential copy may be read.
"""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from hache_trader.domain import GuardError
from hache_trader.telegram import read_secret


class CredentialPermissionsTests(unittest.TestCase):
    TOKEN = "123456789:mock_test_token_only_NOT_A_REAL_SECRET"

    def test_systemd_read_only_group_credential_is_accepted(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            secret = root / "telegram_token"
            secret.write_text(self.TOKEN + "\n", encoding="ascii")
            os.chmod(secret, 0o440)
            with patch.dict(os.environ, {"CREDENTIALS_DIRECTORY": str(root)}):
                self.assertEqual(read_secret(secret), self.TOKEN)

    def test_regular_group_read_token_is_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            secret = Path(d) / "telegram_token"
            secret.write_text(self.TOKEN, encoding="ascii")
            os.chmod(secret, 0o440)
            with patch.dict(os.environ, {"CREDENTIALS_DIRECTORY": str(Path(d) / "other")}):
                with self.assertRaisesRegex(GuardError, "unsafe_secret_permissions"):
                    read_secret(secret)

    def test_world_read_and_group_write_are_always_denied(self):
        with tempfile.TemporaryDirectory() as d:
            secret = Path(d) / "telegram_token"
            secret.write_text(self.TOKEN, encoding="ascii")
            for mode in (0o444, 0o644, 0o660):
                os.chmod(secret, mode)
                with patch.dict(os.environ, {"CREDENTIALS_DIRECTORY": str(Path(d))}):
                    with self.assertRaisesRegex(GuardError, "unsafe_secret_permissions"):
                        read_secret(secret)

    def test_trusted_parent_must_not_be_group_writable(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            secret = root / "telegram_token"
            secret.write_text(self.TOKEN, encoding="ascii")
            os.chmod(secret, 0o440)
            os.chmod(root, 0o770)
            with patch.dict(os.environ, {"CREDENTIALS_DIRECTORY": str(root)}):
                with self.assertRaisesRegex(GuardError, "unsafe_secret_permissions"):
                    read_secret(secret)

    def test_source_owner_only_mode_remains_supported(self):
        with tempfile.TemporaryDirectory() as d:
            secret = Path(d) / "telegram_token"
            secret.write_text(self.TOKEN, encoding="ascii")
            os.chmod(secret, 0o600)
            with patch.dict(os.environ, {"CREDENTIALS_DIRECTORY": ""}):
                self.assertEqual(read_secret(secret), self.TOKEN)

    def test_symlink_never_accepted(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            real = root / "real"
            real.write_text(self.TOKEN, encoding="ascii")
            os.chmod(real, 0o440)
            symlink = root / "telegram_token"
            symlink.symlink_to(real)
            with patch.dict(os.environ, {"CREDENTIALS_DIRECTORY": str(root)}):
                with self.assertRaisesRegex(GuardError, "unsafe_secret_path"):
                    read_secret(symlink)


if __name__ == "__main__":
    unittest.main()
