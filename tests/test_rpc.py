import asyncio
import json
import unittest

from ..codex_rpc import JsonlRpcClient


class FakeWriter:
    def __init__(self):
        self.frames = []
        self.closed = False

    def write(self, data):
        self.frames.append(data)

    async def drain(self):
        return None

    def close(self):
        self.closed = True

    async def wait_closed(self):
        return None


class RpcTests(unittest.IsolatedAsyncioTestCase):
    async def test_pending_response_and_notification_dispatch(self):
        reader = asyncio.StreamReader()
        writer = FakeWriter()
        client = JsonlRpcClient(reader, writer, request_timeout=1)
        notifications = []

        async def on_note(method, params):
            notifications.append((method, params))

        client.subscribe("test/event", on_note)
        client.start()
        request = asyncio.create_task(client.request("echo", {"x": 1}))
        await asyncio.sleep(0)
        sent = json.loads(writer.frames[0])
        self.assertEqual(sent["method"], "echo")
        reader.feed_data(json.dumps({"id": sent["id"], "result": {"ok": True}}).encode() + b"\n")
        reader.feed_data(json.dumps({"method": "test/event", "params": {"n": 1}}).encode() + b"\n")
        self.assertEqual(await request, {"ok": True})
        await asyncio.sleep(0)
        self.assertEqual(notifications, [("test/event", {"n": 1})])
        await client.close()
