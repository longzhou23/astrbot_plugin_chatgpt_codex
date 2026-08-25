import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ..codex_errors import CodexPluginError
from ..codex_service import CodexService


class OAuthCallbackTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.service = CodexService(Path(self.temp_dir.name), {})
        self.addCleanup(self.temp_dir.cleanup)
        self.addAsyncCleanup(self.service.sessions.close)

    async def test_forwards_only_expected_loopback_callback(self):
        self.service._browser_callback_port = 43123
        with patch.object(self.service, "_forward_browser_callback", return_value=204) as forward:
            result = await self.service.submit_browser_callback(
                "http://localhost:43123/auth/callback?code=one-time&state=nonce"
            )
        self.assertEqual(result, {"accepted": True, "awaitingCompletion": True})
        self.assertEqual(forward.call_args.args[0], 43123)
        self.assertTrue(forward.call_args.args[1].startswith("/auth/callback?"))

    async def test_rejects_non_matching_callback_before_network(self):
        self.service._browser_callback_port = 43123
        with (
            patch.object(self.service, "_forward_browser_callback") as forward,
            self.assertRaises(CodexPluginError),
        ):
            await self.service.submit_browser_callback(
                "http://example.test:43123/auth/callback?code=one-time&state=nonce"
            )
        forward.assert_not_called()

    def test_extracts_only_valid_local_callback_listener(self):
        valid = (
            "https://auth.example/authorize?redirect_uri="
            "http%3A%2F%2Flocalhost%3A43123%2Fauth%2Fcallback"
        )
        self.assertEqual(self.service._callback_port_from_auth_url(valid), 43123)
        self.assertIsNone(
            self.service._callback_port_from_auth_url("https://auth.example/authorize")
        )
