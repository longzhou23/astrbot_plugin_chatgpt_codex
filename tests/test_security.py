import unittest

from ..codex_security import redact_text, safe_error


class SecurityTests(unittest.TestCase):
    def test_redacts_token_like_values_and_preserves_safe_text(self):
        value = "Bearer abc.def; refresh_token=refresh-secret user_code=ABCD-1234"
        redacted = redact_text(value)
        self.assertNotIn("abc.def", redacted)
        self.assertNotIn("refresh-secret", redacted)
        self.assertNotIn("ABCD-1234", redacted)
        self.assertIn("Bearer <redacted>", redacted)
        self.assertEqual(safe_error("normal error"), "normal error")

    def test_redacts_oauth_callback_query_values(self):
        redacted = safe_error(
            "callback failed: http://localhost/auth/callback?code=secret-code&state=secret-state"
        )
        self.assertNotIn("secret-code", redacted)
        self.assertNotIn("secret-state", redacted)
        self.assertIn("code=<redacted>", redacted)

    def test_redacts_inline_image_data(self):
        redacted = safe_error("bad image data:image/png;base64,c2Vuc2l0aXZlLWltYWdl")
        self.assertNotIn("c2Vuc2l0aXZl", redacted)
        self.assertIn("<redacted-data-uri>", redacted)
