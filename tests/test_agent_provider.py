import unittest

from ..agent_provider import (
    _conversation_key,
    _is_title_generation_request,
    _normalize_request_inputs,
    _stream_frames,
)


async def event_stream(events):
    for event in events:
        yield event


class AgentProviderContractTests(unittest.IsolatedAsyncioTestCase):
    def test_astrbot_internal_title_request_is_detected(self):
        self.assertTrue(
            _is_title_generation_request(
                "Generate a concise title for the following user query.",
                [],
                (
                    "You are a conversation title generator. "
                    "Generate a concise title in the same language."
                ),
            )
        )

    def test_normal_user_request_is_not_treated_as_title_generation(self):
        self.assertFalse(
            _is_title_generation_request(
                "Help me name this chat", [{"role": "user", "content": "hello"}]
            )
        )

    def test_latest_user_message_is_extracted_from_astrbot_contexts(self):
        prompt, contexts = _normalize_request_inputs(
            None,
            [
                {"role": "system", "content": "persona"},
                {"role": "assistant", "content": "previous"},
                {"role": "user", "content": [{"type": "text", "text": "hello"}]},
            ],
        )
        self.assertEqual(prompt, "hello")
        self.assertEqual(contexts[-1]["role"], "assistant")

    def test_explicit_prompt_is_not_duplicated_in_bootstrap_context(self):
        prompt, contexts = _normalize_request_inputs(
            "hello", [{"role": "user", "content": "hello"}]
        )
        self.assertEqual(prompt, "hello")
        self.assertEqual(contexts, [])

    def test_sessionless_turn_is_rejected_to_prevent_shared_thread(self):
        with self.assertRaises(RuntimeError):
            _conversation_key(None)
        self.assertEqual(_conversation_key("  group:1  "), "group:1")

    async def test_stream_always_ends_with_non_chunk_terminal_response(self):
        frames = [
            frame
            async for frame in _stream_frames(event_stream([{"kind": "final", "text": "hello"}]))
        ]
        self.assertEqual(frames, [("hello", True), ("", False)])

    async def test_status_chunks_do_not_replace_terminal_answer(self):
        frames = [
            frame
            async for frame in _stream_frames(
                event_stream(
                    [
                        {"kind": "status", "text": "working"},
                        {"kind": "final", "text": "done"},
                    ]
                )
            )
        ]
        self.assertEqual(frames[-1], ("", False))


if __name__ == "__main__":
    unittest.main()
