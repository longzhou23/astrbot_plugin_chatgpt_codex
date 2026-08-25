from __future__ import annotations

import sys
import unittest
from pathlib import Path

from astrbot_plugin_chatgpt_codex.process_manager import CodexProcessManager


class ProcessManagerDiagnosticTests(unittest.TestCase):
    def test_diagnostic_accepts_an_executable_path(self) -> None:
        manager = CodexProcessManager(sys.executable, Path.cwd() / "CODEX_HOME-test")

        result = manager.diagnostic()

        self.assertTrue(result["available"])
        self.assertEqual(result["configured"], sys.executable)

    def test_diagnostic_reports_missing_command_without_starting_process(self) -> None:
        manager = CodexProcessManager(
            "codex-command-that-does-not-exist-for-this-test",
            Path.cwd() / "CODEX_HOME-test",
        )

        result = manager.diagnostic()

        self.assertFalse(result["available"])
        self.assertIn("Docker", str(result["error"]))
        self.assertIsNone(manager.process)


if __name__ == "__main__":
    unittest.main()
