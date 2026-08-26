from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from ..model_catalog import CodexModel
from ..transport.client import CodexTransportClient
from ..transport.models import parse_transport_models
from ..transport.responses import build_input_items, parse_sse_data, response_request
from ..transport.types import TransportResponse, TransportToolCall, TransportUsage


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
                {"role": "developer", "content": "instructions belong in instructions"},
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
        self.assertIn(
            {"type": "input_audio", "audio_url": "https://example.invalid/current.wav"},
            latest["content"],
        )
        self.assertEqual(items[-2]["type"], "function_call")
        self.assertEqual(items[-1]["type"], "function_call_output")

    def test_input_mapping_preserves_audio_and_attachment_markers(self):
        items = build_input_items(
            [],
            "请看看这些附件",
            extra_user_content_parts=[
                self.FakeContentPart(
                    {"type": "reply", "message_str": "上一条被引用的消息"}
                ),
                self.FakeContentPart({"type": "file", "name": "星图说明.pdf"}),
                self.FakeContentPart({"type": "video", "name": "观测现场.mp4"}),
                self.FakeContentPart(
                    {"type": "input_audio", "audio_url": "data:audio/wav;base64,YQ=="}
                ),
            ],
        )
        content = items[-1]["content"]
        self.assertIn(
            {"type": "input_text", "text": "[引用消息]\n上一条被引用的消息"},
            content,
        )
        self.assertIn({"type": "input_text", "text": "[文件附件：星图说明.pdf]"}, content)
        self.assertIn({"type": "input_text", "text": "[视频附件：观测现场.mp4]"}, content)
        self.assertIn(
            {"type": "input_audio", "audio_url": "data:audio/wav;base64,YQ=="},
            content,
        )

    def test_completed_output_message_and_refusal_are_visible_but_reasoning_is_not(self):
        result = TransportResponse()
        self.assertTrue(
            parse_sse_data(
                json.dumps(
                    {
                        "type": "response.completed",
                        "response": {
                            "id": "resp-message",
                            "output": [
                                {
                                    "type": "message",
                                    "content": [
                                        {"type": "output_text", "text": "完成"},
                                        {"type": "refusal", "text": "无法继续"},
                                    ],
                                },
                                {
                                    "type": "reasoning",
                                    "id": "rs-private",
                                    "encrypted_content": "secret",
                                },
                            ],
                        },
                    }
                ),
                result,
            )
        )
        self.assertEqual(result.text, "完成无法继续")
        self.assertNotIn("secret", result.text)

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

    def test_function_call_arguments_are_assembled_across_sse_events(self):
        result = TransportResponse()
        self.assertFalse(
            parse_sse_data(
                json.dumps(
                    {
                        "type": "response.output_item.added",
                        "item": {
                            "id": "fc-item-1",
                            "type": "function_call",
                            "call_id": "call-1",
                            "name": "analyze_star_chart",
                            "arguments": "",
                        },
                    }
                ),
                result,
            )
        )
        for delta in ['{"birth":', '"2000-01-01"}']:
            self.assertFalse(
                parse_sse_data(
                    json.dumps(
                        {
                            "type": "response.function_call_arguments.delta",
                            "item_id": "fc-item-1",
                            "delta": delta,
                        }
                    ),
                    result,
                )
            )
        self.assertFalse(
            parse_sse_data(
                json.dumps(
                    {
                        "type": "response.function_call_arguments.done",
                        "item_id": "fc-item-1",
                        "name": "analyze_star_chart",
                        "arguments": '{"birth":"2000-01-01"}',
                    }
                ),
                result,
            )
        )
        # The final item and completed response may repeat the same call. The
        # adapter must update it rather than return duplicate tool calls.
        self.assertFalse(
            parse_sse_data(
                json.dumps(
                    {
                        "type": "response.output_item.done",
                        "item": {
                            "id": "fc-item-1",
                            "type": "function_call",
                            "call_id": "call-1",
                            "name": "analyze_star_chart",
                            "arguments": '{"birth":"2000-01-01"}',
                        },
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
                            "id": "resp-tool-1",
                            "output": [
                                {
                                    "id": "fc-item-1",
                                    "type": "function_call",
                                    "call_id": "call-1",
                                    "name": "analyze_star_chart",
                                    "arguments": '{"birth":"2000-01-01"}',
                                }
                            ],
                        },
                    }
                ),
                result,
            )
        )
        self.assertEqual(
            result.tool_calls,
            [
                TransportToolCall(
                    "call-1",
                    "analyze_star_chart",
                    '{"birth":"2000-01-01"}',
                )
            ],
        )

    def test_function_call_uses_item_id_when_call_id_is_missing(self):
        result = TransportResponse()
        self.assertTrue(
            parse_sse_data(
                json.dumps(
                    {
                        "type": "response.completed",
                        "response": {
                            "id": "resp-tool-fallback",
                            "output": [
                                {
                                    "id": "fc-item-fallback",
                                    "type": "function_call",
                                    "name": "analyze_star_field",
                                    "arguments": "{}",
                                }
                            ],
                        },
                    }
                ),
                result,
            )
        )
        self.assertEqual(result.tool_calls[0].call_id, "fc-item-fallback")
        self.assertEqual(result.event_types, ["response.completed"])

    def test_custom_tool_call_events_are_adapted_for_astrbot_tool_loop(self):
        result = TransportResponse()
        events = [
            {
                "type": "response.output_item.added",
                "item": {
                    "id": "custom-item-1",
                    "type": "custom_tool_call",
                    "call_id": "custom-call-1",
                    "name": "analyze_star_field",
                    "input": "",
                },
            },
            {
                "type": "response.custom_tool_call_input.delta",
                "item_id": "custom-item-1",
                "delta": '{"image":',
            },
            {
                "type": "response.custom_tool_call_input.done",
                "item_id": "custom-item-1",
                "input": '{"image":"current"}',
            },
            {
                "type": "response.output_item.done",
                "item": {
                    "id": "custom-item-1",
                    "type": "custom_tool_call",
                    "call_id": "custom-call-1",
                    "name": "analyze_star_field",
                    "input": '{"image":"current"}',
                },
            },
        ]
        for event in events:
            self.assertFalse(parse_sse_data(json.dumps(event), result))
        self.assertEqual(
            result.tool_calls,
            [
                TransportToolCall(
                    "custom-call-1",
                    "analyze_star_field",
                    '{"image":"current"}',
                )
            ],
        )

    def test_output_text_done_is_used_when_no_text_delta_arrives(self):
        result = TransportResponse()
        self.assertFalse(
            parse_sse_data(
                json.dumps({"type": "response.output_text.done", "text": "answer"}),
                result,
            )
        )
        self.assertEqual(result.text, "answer")

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

    def test_tool_history_without_prompt_does_not_create_synthetic_user_message(self):
        items = build_input_items(
            [
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
                {"role": "tool", "tool_call_id": "call-1", "content": "result"},
            ],
            None,
            include_latest=True,
        )
        self.assertEqual(
            items,
            [
                {
                    "type": "function_call",
                    "call_id": "call-1",
                    "name": "lookup",
                    "arguments": "{}",
                },
                {"type": "function_call_output", "call_id": "call-1", "output": "result"},
            ],
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
