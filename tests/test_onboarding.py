import json
import tempfile
import unittest
from pathlib import Path

from ..codex_service import CodexService


class OnboardingStateTests(unittest.IsolatedAsyncioTestCase):
    async def test_legacy_runtime_state_does_not_hide_onboarding(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir)
            (data_dir / "runtime_settings.json").write_text(
                json.dumps({"model": "auto", "effort": "auto"}),
                encoding="utf-8",
            )

            service = CodexService(data_dir, {})
            try:
                self.assertFalse(service.setup_completed)
            finally:
                await service.sessions.close()

    async def test_explicit_completed_state_is_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir)
            (data_dir / "runtime_settings.json").write_text(
                json.dumps(
                    {"model": "auto", "effort": "auto", "setupCompleted": True}
                ),
                encoding="utf-8",
            )

            service = CodexService(data_dir, {})
            try:
                self.assertTrue(service.setup_completed)
            finally:
                await service.sessions.close()
