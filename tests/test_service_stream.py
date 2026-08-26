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


class FakeTransport:
    def __init__(self):
        self.calls = []

    async def stream_chat(self, **kwargs):
        self.calls.append(kwargs)
        yield {
            "kind": "final",
            "text": f"answer-{len(self.calls)}",
            "response_id": f"resp-{len(self.calls)}",
            "usage": None,
            "tool_calls": [],
        }


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

    def test_transport_instructions_keep_context_persona_and_dedupe_explicit_prompt(self):
        instructions = CodexService._instructions_from_contexts(
            "persona",
            [
                {"role": "system", "content": "persona"},
                {"role": "developer", "content": "Keep the reply short."},
                {"role": "user", "content": "not an instruction"},
            ],
        )
        self.assertEqual(instructions, "persona\n\nKeep the reply short.")

    async def test_transport_sends_persona_when_astrbot_embeds_it_in_contexts(self):
        with tempfile.TemporaryDirectory() as directory:
            service = CodexService(
                Path(directory),
                {"backend_mode": "transport", "turn_timeout": 30},
            )
            fake = FakeTransport()
            service.transport = fake
            try:
                async for _ in service.stream_turn(
                    session_key="session-persona",
                    prompt="hello",
                    contexts=[
                        {"role": "system", "content": "只用一行短回复"},
                    ],
                    system_prompt=None,
                    model="gpt-test",
                ):
                    pass
                self.assertEqual(fake.calls[0]["instructions"], "只用一行短回复")
            finally:
                await service.close()

    async def test_transport_persists_response_state_and_avoids_history_duplication(self):
        with tempfile.TemporaryDirectory() as directory:
            service = CodexService(
                Path(directory),
                {
                    "backend_mode": "transport",
                    "turn_timeout": 30,
                    "max_concurrent_turns": 2,
                },
            )
            fake = FakeTransport()
            service.transport = fake
            try:
                first = [
                    event
                    async for event in service.stream_turn(
                        session_key="session-transport",
                        prompt="second",
                        contexts=[{"role": "user", "content": "first"}],
                        system_prompt="persona",
                        model="gpt-test",
                    )
                ]
                second = [
                    event
                    async for event in service.stream_turn(
                        session_key="session-transport",
                        prompt="third",
                        contexts=[
                            {"role": "user", "content": "first"},
                            {"role": "assistant", "content": "answer-1"},
                        ],
                        system_prompt="persona",
                        model="gpt-test",
                    )
                ]

                self.assertEqual(first, [{"kind": "final", "text": "answer-1", "reasoning_signature": None}])
                self.assertEqual(second, [{"kind": "final", "text": "answer-2", "reasoning_signature": None}])
                self.assertEqual(fake.calls[0]["previous_response_id"], None)
                self.assertEqual(len(fake.calls[0]["input_items"]), 2)
                self.assertEqual(fake.calls[1]["previous_response_id"], "resp-1")
                self.assertEqual(len(fake.calls[1]["input_items"]), 1)
                self.assertEqual(fake.calls[1]["input_items"][0]["content"][0]["text"], "third")
                record = await service.sessions.get("session-transport")
                self.assertEqual(record["response_id"], "resp-2")
            finally:
                await service.close()

    async def test_transport_forwards_tool_context_after_previous_response(self):
        with tempfile.TemporaryDirectory() as directory:
            service = CodexService(
                Path(directory),
                {"backend_mode": "transport", "turn_timeout": 30},
            )
            fake = FakeTransport()
            service.transport = fake
            try:
                async for _ in service.stream_turn(
                    session_key="session-tools",
                    prompt="use a tool",
                    model="gpt-test",
                ):
                    pass
                async for _ in service.stream_turn(
                    session_key="session-tools",
                    prompt=None,
                    contexts=[
                        {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call-1",
                                    "function": {"name": "lookup", "arguments": "{}"},
                                }
                            ],
                        },
                        {"role": "tool", "tool_call_id": "call-1", "content": "tool result"},
                    ],
                    model="gpt-test",
                ):
                    pass
                self.assertEqual(fake.calls[1]["previous_response_id"], "resp-1")
                self.assertEqual(
                    fake.calls[1]["input_items"],
                    [
                        {
                            "type": "function_call_output",
                            "call_id": "call-1",
                            "output": "tool result",
                        }
                    ],
                )
            finally:
                await service.close()


if __name__ == "__main__":
    unittest.main()
