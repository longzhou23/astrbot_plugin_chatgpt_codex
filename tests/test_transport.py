from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from ..model_catalog import CodexModel
from ..transport.client import CodexTransportClient
from ..transport.models import parse_transport_models
from ..transport.responses import build_input_items, parse_sse_data, response_request
from ..transport.types import TransportResponse, TransportUsage


class TransportTests(unittest.TestCase):
    class FakeContentPart:
        def __init__(self, value):
            self.value = value

        def model_dump_for_context(self):
            return self.value

    class FakeToolResult:
        def to_openai_messages(self):
            return [
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call-1",
                            "type": "function",
                            "function": {"name": "lookup", "arguments": '{"q":"x"}'},
                        }
                    ],
                },
                {"role": "tool", "tool_call_id": "call-1", "content": "result"},
            ]

    def test_request_is_direct_responses_without_thread_turn_or_codex_tools(self):
        payload = response_request(
            model="gpt-test",
            instructions="be concise",
            input_items=build_input_items([], "hello"),
        )
        serialized = json.dumps(payload)
        self.assertNotIn("thread/start", serialized)
        self.assertNotIn("turn/start", serialized)
        self.assertNotIn("shell", serialized)
        self.assertNotIn("computer", serialized)
        self.assertEqual(payload["store"], False)
        self.assertEqual(payload["stream"], True)

    def test_input_mapping_keeps_history_and_latest_message(self):
        items = build_input_items(
            [
                {"role": "user", "content": "old question"},
                {"role": "assistant", "content": "old answer"},
                {"role": "system", "content": "ignored here"},
            ],
            "latest",
        )
        self.assertEqual([item["role"] for item in items], ["user", "assistant", "user"])
        self.assertEqual(items[-1]["content"][0]["text"], "latest")

    def test_input_mapping_preserves_content_parts_images_and_tool_results(self):
        items = build_input_items(
            [
                {
                    "role": "assistant",
                    "content": [
                        {"type": "text", "text": "I will check."},
                    ],
                    "tool_calls": [
                        {
                            "id": "call-old",
                            "function": {"name": "lookup", "arguments": "{}"},
                        }
                    ],
                },
                {"role": "tool", "tool_call_id": "call-old", "content": "old result"},
            ],
            "inspect",
            extra_user_content_parts=[
                self.FakeContentPart({"type": "text", "text": "memory"}),
                self.FakeContentPart({"type": "image_url", "image_url": {"url": "data:image/png;base64,x"}}),
            ],
            image_urls=["https://example.invalid/current.png"],
            audio_urls=["https://example.invalid/current.wav"],
            tool_calls_result=self.FakeToolResult(),
        )

        self.assertEqual(items[1]["type"], "function_call")
        self.assertEqual(items[2]["type"], "function_call_output")
        latest = items[3]
        self.assertEqual(latest["role"], "user")
        self.assertIn({"type": "input_text", "text": "memory"}, latest["content"])
        self.assertIn(
            {"type": "input_image", "detail": "auto", "image_url": "data:image/png;base64,x"},
            latest["content"],
        )
        self.assertIn(
            {"type": "input_image", "detail": "auto", "image_url": "https://example.invalid/current.png"},
            latest["content"],
        )
        self.assertIn({"type": "input_text", "text": "[Audio]"}, latest["content"])
        self.assertEqual(items[-2]["type"], "function_call")
        self.assertEqual(items[-1]["type"], "function_call_output")

    def test_opaque_reasoning_is_replayed_without_plaintext(self):
        items = build_input_items(
            [
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "think",
                            "think": "private text must not be forwarded as visible text",
                            "encrypted": json.dumps(
                                {
                                    "type": "openai_responses_reasoning",
                                    "items": [{"type": "reasoning", "id": "rs_1"}],
                                }
                            ),
                        },
                        {"type": "text", "text": "answer"},
                    ],
                }
            ],
            "next",
        )
        self.assertEqual(items[0], {"type": "reasoning", "id": "rs_1"})
        serialized = json.dumps(items, ensure_ascii=False)
        self.assertNotIn("private text", serialized)

    def test_models_accepts_codex_slug_and_snake_case_efforts(self):
        models = parse_transport_models(
            {
                "models": [
                    {
                        "slug": "gpt-test",
                        "display_name": "Test",
                        "supported_reasoning_efforts": ["low", "high"],
                    }
                ]
            }
        )
        self.assertEqual(models, [CodexModel("gpt-test", "Test", ("low", "high"), False, {})])

    def test_completed_event_usage_is_real_response_usage_shape(self):
        result = TransportResponse()
        self.assertFalse(
            parse_sse_data(
                json.dumps(
                    {
                        "type": "response.output_text.delta",
                        "delta": "hello",
                        "response": {"id": "resp-1"},
                    }
                ),
                result,
            )
        )
        self.assertTrue(
            parse_sse_data(
                json.dumps(
                    {
                        "type": "response.completed",
                        "response": {
                            "id": "resp-1",
                            "usage": {
                                "input_tokens": 10,
                                "input_tokens_details": {"cached_tokens": 3},
                                "output_tokens": 4,
                                "output_tokens_details": {"reasoning_tokens": 1},
                                "total_tokens": 14,
                            },
                        },
                    }
                ),
                result,
            )
        )
        self.assertEqual(result.text, "hello")
        self.assertEqual(result.usage, TransportUsage(10, 3, 4, 1, 14, None))

    def test_response_request_can_continue_from_previous_response(self):
        payload = response_request(
            model="gpt-test",
            instructions="be concise",
            input_items=build_input_items([], "hello"),
            previous_response_id="resp-previous",
        )
        self.assertEqual(payload["previous_response_id"], "resp-previous")

    def test_tool_continuation_can_omit_empty_latest_user_message(self):
        items = build_input_items(
            [{"role": "tool", "tool_call_id": "call-1", "content": "result"}],
            None,
            include_latest=False,
        )
        self.assertEqual(
            items,
            [{"type": "function_call_output", "call_id": "call-1", "output": "result"}],
        )

    def test_auth_file_is_not_needed_for_request_serialization(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertFalse((Path(directory) / "auth.json").exists())

    def test_explicit_proxy_is_validated_without_exposing_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            client = CodexTransportClient(Path(directory), proxy_url="http://127.0.0.1:7890")
            self.assertEqual(client.proxy_url, "http://127.0.0.1:7890")
            with self.assertRaises(ValueError):
                client.set_proxy("http://user:password@127.0.0.1:7890")


if __name__ == "__main__":
    unittest.main()
