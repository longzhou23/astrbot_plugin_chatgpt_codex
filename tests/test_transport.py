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
