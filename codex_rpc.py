"""Async JSONL JSON-RPC 2.0-ish client for ``codex app-server --stdio``."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from .codex_errors import CodexRPCError, CodexTimeoutError, CodexTransportError

NotificationHandler = Callable[[str, dict[str, Any]], Awaitable[None] | None]
ServerRequestHandler = Callable[[int, str, dict[str, Any]], Awaitable[Any] | Any]


class JsonlRpcClient:
    """One connection, one reader task, and a concurrency-safe pending table."""

    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        *,
        logger: logging.Logger | None = None,
        request_timeout: float = 30.0,
    ) -> None:
        self._reader = reader
        self._writer = writer
        self._logger = logger or logging.getLogger(__name__)
        self._request_timeout = request_timeout
        self._next_id = 0
        self._id_lock = asyncio.Lock()
        self._write_lock = asyncio.Lock()
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._handlers: dict[str, list[NotificationHandler]] = {}
        self._server_request_handler: ServerRequestHandler | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._closed = False
        self._close_error: Exception | None = None

    @property
    def closed(self) -> bool:
        return self._closed

    def start(self) -> None:
        if self._reader_task is None:
            self._reader_task = asyncio.create_task(self._read_loop(), name="codex-rpc-reader")

    def set_server_request_handler(self, handler: ServerRequestHandler | None) -> None:
        self._server_request_handler = handler

    def subscribe(self, method: str, handler: NotificationHandler) -> Callable[[], None]:
        self._handlers.setdefault(method, []).append(handler)

        def unsubscribe() -> None:
            handlers = self._handlers.get(method, [])
            with contextlib.suppress(ValueError):
                handlers.remove(handler)

        return unsubscribe

    async def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        await self._send({"jsonrpc": "2.0", "method": method, "params": params or {}})

    async def request(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        timeout: float | None = None,
    ) -> Any:
        if self._closed:
            raise CodexTransportError(str(self._close_error or "RPC connection is closed"))
        async with self._id_lock:
            self._next_id += 1
            request_id = self._next_id
        loop = asyncio.get_running_loop()
        future: asyncio.Future[Any] = loop.create_future()
        self._pending[request_id] = future
        try:
            await self._send(
                {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}}
            )
            try:
                return await asyncio.wait_for(future, timeout or self._request_timeout)
            except TimeoutError as exc:
                raise CodexTimeoutError(f"RPC request timed out: {method}") from exc
        finally:
            self._pending.pop(request_id, None)

    async def _send(self, message: dict[str, Any]) -> None:
        line = (json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
        async with self._write_lock:
            if self._closed:
                raise CodexTransportError(str(self._close_error or "RPC connection is closed"))
            self._writer.write(line)
            try:
                await self._writer.drain()
            except (ConnectionError, OSError) as exc:
                await self.close(exc)
                raise CodexTransportError("RPC write failed") from exc

    async def _read_loop(self) -> None:
        error: Exception | None = None
        try:
            while True:
                raw = await self._reader.readline()
                if not raw:
                    raise EOFError("Codex app-server closed stdout")
                try:
                    message = json.loads(raw)
                except json.JSONDecodeError:
                    self._logger.warning("Ignored malformed JSONL frame from Codex app-server")
                    continue
                if not isinstance(message, dict):
                    continue
                if "id" in message and ("result" in message or "error" in message):
                    self._dispatch_response(message)
                elif "method" in message:
                    await self._dispatch_notification(message)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            error = exc
            if not self._closed:
                self._logger.warning("Codex RPC reader stopped: %s", exc)
        finally:
            await self.close(error)

    def _dispatch_response(self, message: dict[str, Any]) -> None:
        request_id = message.get("id")
        if not isinstance(request_id, int):
            return
        future = self._pending.get(request_id)
        if future is None or future.done():
            return
        if "error" in message:
            error = message.get("error") or {}
            future.set_exception(
                CodexRPCError(
                    error.get("code"), error.get("message", "Codex RPC error"), error.get("data")
                )
            )
        else:
            future.set_result(message.get("result"))

    async def _dispatch_notification(self, message: dict[str, Any]) -> None:
        method = message.get("method")
        if not isinstance(method, str):
            return
        params = message.get("params")
        if not isinstance(params, dict):
            params = {}
        # The app-server may send approval/user-input requests as server requests.
        if "id" in message:
            request_id = message.get("id")
            if isinstance(request_id, int) and self._server_request_handler:
                try:
                    result = self._server_request_handler(request_id, method, params)
                    if asyncio.iscoroutine(result):
                        result = await result
                    await self._send({"jsonrpc": "2.0", "id": request_id, "result": result or {}})
                except Exception as exc:
                    await self._send(
                        {
                            "jsonrpc": "2.0",
                            "id": request_id,
                            "error": {"code": -32000, "message": str(exc)},
                        }
                    )
            return
        for handler in tuple(self._handlers.get(method, ())):
            try:
                result = handler(method, params)
                if asyncio.iscoroutine(result):
                    await result
            except Exception:
                self._logger.exception("Notification handler failed for %s", method)

    async def close(self, error: Exception | None = None) -> None:
        if self._closed:
            return
        self._closed = True
        self._close_error = error
        for future in tuple(self._pending.values()):
            if not future.done():
                future.set_exception(CodexTransportError(str(error or "RPC connection closed")))
        self._pending.clear()
        if self._reader_task and self._reader_task is not asyncio.current_task():
            self._reader_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reader_task
        self._writer.close()
        with contextlib.suppress(Exception):
            await self._writer.wait_closed()
