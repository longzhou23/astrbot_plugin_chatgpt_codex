import tempfile
import unittest
from pathlib import Path

from ..session_store import SessionStore


class SessionStoreTests(unittest.IsolatedAsyncioTestCase):
    async def test_mapping_and_per_session_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SessionStore(Path(directory) / "sessions.sqlite3")
            self.assertIsNone(await store.get("s1"))
            await store.put(
                "s1",
                "thr_1",
                bootstrapped=False,
                model="gpt-a",
                prompt_version="pv1",
            )
            self.assertEqual((await store.get("s1"))["thread_id"], "thr_1")
            self.assertEqual((await store.get("s1"))["prompt_version"], "pv1")
            await store.put(
                "s1",
                "thr_1",
                bootstrapped=True,
                model="gpt-a",
                prompt_version="pv1",
                increment_turn=True,
            )
            self.assertEqual((await store.get("s1"))["turn_count"], 1)
            self.assertIs(store.lock_for("s1"), store.lock_for("s1"))
            self.assertTrue(await store.reset("s1"))
            self.assertFalse(await store.reset("s1"))
            await store.close()
