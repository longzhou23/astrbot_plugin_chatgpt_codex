import tempfile
import unittest
from pathlib import Path

from ..codex_errors import CodexRPCError
from ..codex_service import CodexService


class FakeRpc:
    def __init__(self, notifications):
        self.notifications = notifications
        self.handlers = {}
        self.interrupts = []

    def subscribe(self, method, handler):
        self.handlers.setdefault(method, []).append(handler)

        def unsubscribe():
            self.handlers[method].remove(handler)

        return unsubscribe

    async def _emit(self, method, params):
        for handler in list(self.handlers.get(method, [])):
            await handler(method, params)

    async def request(self, method, params, timeout=None):
        del timeout
        if method == "turn/start":
            for event_method, event_params in self.notifications:
                await self._emit(event_method, event_params)
            return {"turn": {"id": "turn-current", "status": "inProgress", "items": []}}
        if method == "turn/interrupt":
            self.interrupts.append(params)
            return {}
        raise AssertionError(f"Unexpected RPC method: {method}")


class ServiceStreamingTests(unittest.IsolatedAsyncioTestCase):
    async def _service(self, rpc):
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        service = CodexService(
            Path(temp_dir.name),
            {
                "backend_mode": "app_server",
                "turn_timeout": 30,
                "max_concurrent_turns": 2,
                "show_tool_status": False,
            },
        )

        async def fake_connect():
            return rpc

        async def fake_thread_for(session_key, *, model, developer_instructions, prompt_version):
            del session_key, model, developer_instructions, prompt_version
            return "thread-current", True

        service._connect = fake_connect
        service._thread_for = fake_thread_for
        self.addAsyncCleanup(service.sessions.close)
        return service

    async def test_retry_and_replayed_deltas_emit_only_authoritative_final(self):
        current = {"threadId": "thread-current", "turnId": "turn-current"}
        notifications = [
            (
                "item/agentMessage/delta",
                {"threadId": "thread-current", "turnId": "turn-stale", "delta": "stale"},
            ),
            (
                "error",
                {
                    **current,
                    "willRetry": True,
                    "error": {
                        "message": "Reconnecting... 2/5",
                        "codexErrorInfo": {"responseStreamDisconnected": {}},
                    },
                },
            ),
            ("item/agentMessage/delta", {**current, "itemId": "msg-1", "delta": "Hi"}),
            ("item/agentMessage/delta", {**current, "itemId": "msg-1", "delta": "Hi"}),
            (
                "item/completed",
                {
                    **current,
                    "item": {
                        "id": "msg-1",
                        "type": "agentMessage",
                        "phase": "final_answer",
                        "text": "Hi! How can I help?",
                    },
                },
            ),
            (
                "turn/completed",
                {
                    "threadId": "thread-current",
                    "turn": {
                        "id": "turn-current",
                        "status": "completed",
                        "items": [],
                    },
                },
            ),
        ]
        rpc = FakeRpc(notifications)
        service = await self._service(rpc)

        events = [
            event
            async for event in service.stream_turn(
                session_key="session-1", prompt="hello", model="test-model"
            )
        ]

        self.assertEqual(events, [{"kind": "final", "text": "Hi! How can I help?"}])
        self.assertEqual(rpc.interrupts, [])

    async def test_non_retryable_error_interrupts_active_turn(self):
        notifications = [
            (
                "error",
                {
                    "threadId": "thread-current",
                    "turnId": "turn-current",
                    "willRetry": False,
                    "error": {"message": "upstream failed", "codexErrorInfo": "other"},
                },
            )
        ]
        rpc = FakeRpc(notifications)
        service = await self._service(rpc)

        with self.assertRaises(CodexRPCError):
            async for _ in service.stream_turn(
                session_key="session-2", prompt="hello", model="test-model"
            ):
                pass

        self.assertEqual(
            rpc.interrupts,
            [{"threadId": "thread-current", "turnId": "turn-current"}],
        )

    def test_final_answer_phase_wins_and_commentary_is_hidden(self):
        items = [
            {"type": "agentMessage", "phase": "commentary", "text": "Working..."},
            {"type": "agentMessage", "phase": None, "text": "legacy fallback"},
            {"type": "agentMessage", "phase": "final_answer", "text": "Final answer"},
        ]
        self.assertEqual(CodexService._final_agent_text(items), "Final answer")


if __name__ == "__main__":
    unittest.main()
