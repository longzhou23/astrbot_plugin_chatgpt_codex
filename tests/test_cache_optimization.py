import tempfile
import unittest
from pathlib import Path

from ..codex_service import CodexService


class CacheOptimizationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.service = CodexService(Path(self.temp_dir.name), {})
        self.addCleanup(self.temp_dir.cleanup)
        self.addAsyncCleanup(self.service.sessions.close)

    def test_prompt_version_is_byte_stable(self):
        versions = {self.service.prompt_version(" persona\r\n") for _ in range(100)}
        self.assertEqual(len(versions), 1)
        self.assertNotEqual(
            self.service.prompt_version("persona"),
            self.service.prompt_version("different persona"),
        )

    async def test_active_thread_is_reused_without_repeated_resume(self):
        calls = []

        async def request(method, params=None, *, timeout=30):
            del timeout
            calls.append((method, params))
            if method == "thread/start":
                return {"thread": {"id": "thread-a"}}
            return {}

        self.service._request = request
        self.service._rpc = type("ActiveRpc", (), {"closed": False})()
        prompt_version = self.service.prompt_version("persona")
        first = await self.service._thread_for(
            "conversation-a",
            model="gpt-a",
            developer_instructions="persona",
            prompt_version=prompt_version,
        )
        second = await self.service._thread_for(
            "conversation-a",
            model="gpt-a",
            developer_instructions="persona",
            prompt_version=prompt_version,
        )
        self.assertEqual(first[0], second[0])
        self.assertEqual([method for method, _ in calls], ["thread/start", "thread/name/set"])

        self.service._active_threads.clear()
        await self.service._thread_for(
            "conversation-a",
            model="gpt-a",
            developer_instructions="persona",
            prompt_version=prompt_version,
        )
        self.assertEqual([method for method, _ in calls][-1], "thread/resume")

    async def test_different_conversations_do_not_share_mapping(self):
        next_id = iter(("thread-a", "thread-b"))

        async def request(method, params=None, *, timeout=30):
            del params, timeout
            if method == "thread/start":
                return {"thread": {"id": next(next_id)}}
            return {}

        self.service._request = request
        prompt_version = self.service.prompt_version("persona")
        thread_a, _ = await self.service._thread_for(
            "conversation-a",
            model="gpt-a",
            developer_instructions="persona",
            prompt_version=prompt_version,
        )
        thread_b, _ = await self.service._thread_for(
            "conversation-b",
            model="gpt-a",
            developer_instructions="persona",
            prompt_version=prompt_version,
        )
        self.assertNotEqual(thread_a, thread_b)


if __name__ == "__main__":
    unittest.main()
