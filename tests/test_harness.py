import tempfile
import unittest
from pathlib import Path

from ..codex_service import CodexService
from ..harness import LIGHTWEIGHT_BASE_INSTRUCTIONS, lightweight_config


class HarnessPolicyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.service = CodexService(Path(self.temp_dir.name), {})
        self.addCleanup(self.temp_dir.cleanup)
        self.addAsyncCleanup(self.service.sessions.close)

    async def test_lightweight_thread_replaces_base_instructions(self):
        calls = []

        async def request(method, params=None, *, timeout=30):
            del timeout
            calls.append((method, params or {}))
            if method == "thread/start":
                return {"thread": {"id": "lightweight-thread"}}
            return {}

        self.service._request = request
        prompt_version = self.service.prompt_version("persona")
        await self.service._thread_for(
            "conversation-a",
            model="gpt-a",
            developer_instructions="persona",
            prompt_version=prompt_version,
        )

        params = calls[0][1]
        self.assertEqual(params["baseInstructions"], LIGHTWEIGHT_BASE_INSTRUCTIONS)
        self.assertEqual(params["config"], lightweight_config())
        self.assertNotIn("dynamicTools", params)

    async def test_codex_mode_keeps_server_defaults(self):
        self.service.config["harness_mode"] = "codex"
        calls = []

        async def request(method, params=None, *, timeout=30):
            del timeout
            calls.append((method, params or {}))
            if method == "thread/start":
                return {"thread": {"id": "codex-thread"}}
            return {}

        self.service._request = request
        prompt_version = self.service.prompt_version("persona")
        await self.service._thread_for(
            "conversation-a",
            model="gpt-a",
            developer_instructions="persona",
            prompt_version=prompt_version,
        )

        params = calls[0][1]
        self.assertNotIn("baseInstructions", params)
        self.assertNotIn("config", params)

    def test_lightweight_prompt_is_short_and_stable(self):
        self.assertLess(len(LIGHTWEIGHT_BASE_INSTRUCTIONS.split()), 100)
        self.assertEqual(lightweight_config(), lightweight_config())

    async def test_prompt_debug_never_contains_raw_base_instructions(self):
        self.service._last_turn = {
            "prompt_version": "redacted-version",
            "context_diagnostics": {"latest_user_chars": 2},
        }
        debug = await self.service.prompt_debug()
        self.assertEqual(debug["base_instructions_chars"], len(LIGHTWEIGHT_BASE_INSTRUCTIONS))
        self.assertNotIn(LIGHTWEIGHT_BASE_INSTRUCTIONS, str(debug))
        self.assertEqual(debug["last_turn_prompt_version"], "redacted-version")


if __name__ == "__main__":
    unittest.main()
