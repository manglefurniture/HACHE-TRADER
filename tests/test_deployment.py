"""Static regressions for the *uninstalled* simulation-only service templates."""
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
SERVICES = ROOT / "deployment" / "systemd"


class DeploymentTemplateTests(unittest.TestCase):
    def test_telegram_service_isolation_and_credentials(self):
        service = (SERVICES / "hache-trader-telegram-paper.service").read_text()
        for required in (
            "DynamicUser=yes", "NoNewPrivileges=yes", "ProtectSystem=strict",
            "StateDirectory=hache-trader-paper", "StateDirectoryMode=0700",
            "LoadCredential=telegram_token:/etc/hache-trader/telegram_token",
            "EnvironmentFile=/etc/hache-trader/telegram.env",
            "serve --telegram --state-dir /var/lib/hache-trader-paper",
        ):
            self.assertIn(required, service)
        for forbidden in ("User=root", "ExecStartPre=/bin/sh", "--live", "API_SECRET=",
                          "StrictHostKeyChecking=no", "ExecStart=/usr/bin/curl"):
            self.assertNotIn(forbidden, service)

    def test_only_one_service_polls_telegram(self):
        paper = (SERVICES / "hache-trader-paper.service").read_text()
        telegram = (SERVICES / "hache-trader-telegram-paper.service").read_text()
        self.assertNotIn("serve --telegram", paper)
        self.assertIn("serve --telegram", telegram)
        self.assertNotIn("run --telegram", telegram)
        self.assertIn("run --state-dir /var/lib/hache-trader-paper", paper)
        self.assertIn("serve --telegram --state-dir /var/lib/hache-trader-paper", telegram)


if __name__ == "__main__":
    unittest.main()
