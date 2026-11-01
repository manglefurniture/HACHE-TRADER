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
            "observe --telegram --state-dir /var/lib/hache-trader-paper",
        ):
            self.assertIn(required, service)
        for forbidden in ("User=root", "ExecStartPre=/bin/sh", "--live", "API_SECRET=",
                          "StrictHostKeyChecking=no", "ExecStart=/usr/bin/curl"):
            self.assertNotIn(forbidden, service)

    def test_only_one_service_polls_telegram(self):
        paper = (SERVICES / "hache-trader-paper.service").read_text()
        telegram = (SERVICES / "hache-trader-telegram-paper.service").read_text()
        self.assertNotIn("observe --telegram", paper)
        self.assertIn("observe --telegram", telegram)
        self.assertNotIn("run --telegram", telegram)
        self.assertNotIn("hache_trader serve --telegram", telegram)
        self.assertIn("run --state-dir /var/lib/hache-trader-paper", paper)
        self.assertIn("observe --telegram --state-dir /var/lib/hache-trader-paper", telegram)

    def test_upgrade_preserves_ledger_and_restarts_old_service_on_failure(self):
        script = (ROOT / "deployment" / "upgrade-telegram-observer.sh").read_text()
        for required in (
            "sha256sum --check --status",
            "ARCHIVE_STRUCTURE_OK",
            "LEDGER_BACKUP_OK",
            "ln -sfnT",
            'systemctl stop "$unit"',
            'systemctl start "$unit"',
            'cp -- "$backup_dir/previous.service" "$source_unit"',
            "observer.py",
            "hache_trader observe --telegram",
        ):
            self.assertIn(required, script)
        self.assertNotIn("StrictHostKeyChecking=no", script)
        self.assertNotIn("chmod 777", script)
        self.assertNotIn("curl -k", script)
        self.assertNotIn("rm -rf /var/lib", script)



if __name__ == "__main__":
    unittest.main()
