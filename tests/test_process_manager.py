from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

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

    def test_login_proxy_is_applied_without_exposing_it_in_diagnostics(self) -> None:
        manager = CodexProcessManager(
            sys.executable,
            Path.cwd() / "CODEX_HOME-test",
            proxy_url="http://127.0.0.1:7890",
        )

        env = manager._subprocess_environment()
        diagnostic = manager.diagnostic()

        self.assertEqual(env["HTTPS_PROXY"], "http://127.0.0.1:7890")
        self.assertEqual(env["https_proxy"], "http://127.0.0.1:7890")
        self.assertTrue(diagnostic["proxyConfigured"])
        self.assertNotIn("7890", str(diagnostic))

    def test_login_proxy_rejects_credentials(self) -> None:
        with self.assertRaises(ValueError):
            CodexProcessManager(
                sys.executable,
                Path.cwd() / "CODEX_HOME-test",
                proxy_url="http://user:password@127.0.0.1:7890",
            )

    def test_system_proxy_mode_inherits_process_environment(self) -> None:
        manager = CodexProcessManager(
            sys.executable,
            Path.cwd() / "CODEX_HOME-test",
            use_system_proxy=True,
        )

        with patch.dict(
            "os.environ",
            {"HTTPS_PROXY": "http://system-proxy:7890", "HTTP_PROXY": "http://system-proxy:7890"},
            clear=False,
        ):
            env = manager._subprocess_environment()

        self.assertEqual(env["HTTPS_PROXY"], "http://system-proxy:7890")
        self.assertEqual(env["HTTP_PROXY"], "http://system-proxy:7890")

    def test_disabled_system_proxy_is_removed_from_subprocess_environment(self) -> None:
        manager = CodexProcessManager(
            sys.executable,
            Path.cwd() / "CODEX_HOME-test",
            use_system_proxy=False,
        )

        with patch.dict(
            "os.environ",
            {"HTTPS_PROXY": "http://system-proxy:7890", "ALL_PROXY": "http://system-proxy:7890"},
            clear=False,
        ):
            env = manager._subprocess_environment()

        self.assertNotIn("HTTPS_PROXY", env)
        self.assertNotIn("ALL_PROXY", env)


if __name__ == "__main__":
    unittest.main()
