import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
README = ROOT / "README.md"
WORKFLOW = ROOT / ".github" / "workflows" / "monitor.yml"


class OperationsDocumentationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.readme = README.read_text(encoding="utf-8")
        cls.workflow = WORKFLOW.read_text(encoding="utf-8")

    def test_runbook_names_required_telegram_secrets(self):
        for secret in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
            with self.subTest(secret=secret):
                self.assertIn(secret, self.readme)
                self.assertIn(f"secrets.{secret}", self.workflow)

    def test_runbook_documents_opt_in_connectivity_test(self):
        self.assertIn("send_test_alert", self.readme)
        self.assertIn("Test Telegram delivery", self.workflow)
        self.assertIn("workflow_dispatch", self.workflow)

    def test_runbook_explains_green_run_is_not_delivery_proof(self):
        self.assertIn("does **not** by itself prove Telegram accepted a message", self.readme)

    def test_runbook_warns_against_committing_secrets(self):
        self.assertIn("Never put either value in", self.readme)
        for path in ("sources.txt", "dashboard files", "issue comments"):
            self.assertIn(path, self.readme)


if __name__ == "__main__":
    unittest.main()
