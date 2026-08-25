import unittest

from ..codex_service import CodexService


class AccountSafetyTests(unittest.TestCase):
    def test_accepts_https_avatar_without_userinfo_or_fragment(self):
        self.assertEqual(
            CodexService._safe_avatar_url(
                {"picture": "https://cdn.example.test/avatar.png"}
            ),
            "https://cdn.example.test/avatar.png",
        )

    def test_rejects_non_public_avatar_values(self):
        for value in (
            "http://cdn.example.test/avatar.png",
            "data:image/png;base64,secret",
            "https://user:password@example.test/avatar.png",
            "https://example.test/avatar.png#token",
            {"url": "https://example.test/avatar.png"},
        ):
            self.assertIsNone(CodexService._safe_avatar_url({"avatarUrl": value}))
