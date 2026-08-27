import unittest

from ..agent_provider import (
    _collect_provider_response,
    _conversation_key,
    _ensure_supported_modalities,
    _has_non_text_content,
    _is_title_generation_request,
    _normalize_request_inputs,
    _stream_frames,
    _stream_provider_responses,
)
from ..transport.types import TransportError


async def event_stream(events):
    for event in events:
        yield event


class AgentProviderContractTests(unittest.IsolatedAsyncioTestCase):
    def test_old_provider_modalities_are_migrated_with_tool_use(self):
        provider_config = {"modalities": ["text", "image"]}

        modalities = _ensure_supported_modalities(provider_config)

        self.assertEqual(modalities, ["text", "image", "audio", "tool_use"])
        self.assertEqual(provider_config["modalities"], modalities)

    def test_missing_provider_modalities_receive_all_supported_values(self):
        provider_config = {}

        modalities = _ensure_supported_modalities(provider_config)

        self.assertEqual(modalities, ["text", "image", "audio", "tool_use"])

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

    def test_output_text_parts_are_read_when_extracting_latest_user_message(self):
        prompt, contexts = _normalize_request_inputs(
            None,
            [{"role": "user", "content": [{"type": "output_text", "text": "hello"}]}],
        )
        self.assertEqual(prompt, "hello")
        self.assertEqual(contexts, [])

    def test_multimodal_current_message_is_not_removed_from_contexts(self):
        prompt, contexts = _normalize_request_inputs(
            None,
            [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "分析这张图"},
                        {
                            "type": "image_url",
                            "image_url": {"url": "data:image/png;base64,x"},
                        },
                    ],
                }
            ],
        )
        self.assertIsNone(prompt)
        self.assertEqual(len(contexts), 1)
        self.assertTrue(_has_non_text_content(contexts[0]["content"]))

    def test_sessionless_turn_gets_a_unique_ephemeral_thread(self):
        first = _conversation_key(None)
        second = _conversation_key(None)
        self.assertTrue(first.startswith("__astrbot_ephemeral__:"))
        self.assertTrue(second.startswith("__astrbot_ephemeral__:"))
        self.assertNotEqual(first, second)
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

    async def test_non_streaming_adapter_preserves_tool_calls(self):
        response = await _collect_provider_response(
            event_stream(
                [
                    {
                        "kind": "tool_call",
                        "tool_calls": [
                            {
                                "call_id": "call-search-1",
                                "name": "web_search",
                                "arguments": '{"query":"latest"}',
                            }
                        ],
                    }
                ]
            )
        )
        self.assertEqual(response.role, "tool")
        self.assertEqual(response.tools_call_name, ["web_search"])
        self.assertEqual(response.tools_call_args, [{"query": "latest"}])
        self.assertEqual(response.tools_call_ids, ["call-search-1"])

    async def test_streaming_adapter_rejects_empty_terminal_response(self):
        with self.assertRaises(TransportError):
            _ = [
                response
                async for response in _stream_provider_responses(
                    event_stream([{"kind": "final", "text": ""}])
                )
            ]


if __name__ == "__main__":
    unittest.main()
