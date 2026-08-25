from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import shlex
import stat
from pathlib import Path

from .codex_errors import CodexProcessError
from .codex_security import redact_text


class CodexProcessManager:
    """Own an isolated long-lived ``codex app-server --stdio`` process."""

    def __init__(
        self,
        codex_path: str,
        codex_home: Path,
        *,
        logger: logging.Logger | None = None,
        restart_limit: int = 5,
        force_http_transport: bool = True,
    ) -> None:
        self.codex_path = codex_path or "codex"
        self.codex_home = codex_home
        self.logger = logger or logging.getLogger(__name__)
        self.restart_limit = restart_limit
        self.force_http_transport = force_http_transport
        self.process: asyncio.subprocess.Process | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._monitor_task: asyncio.Task[None] | None = None
        self._stop_requested = False
        self._lock = asyncio.Lock()
        self._restart_count = 0
        self.last_exit_code: int | None = None
        self.last_error: str | None = None

    @property
    def healthy(self) -> bool:
        return self.process is not None and self.process.returncode is None

    @property
    def status(self) -> str:
        if self.healthy:
            return "healthy"
        if self._stop_requested:
            return "stopped"
        return "offline"

    def _prepare_home(self) -> None:
        self.codex_home.mkdir(parents=True, exist_ok=True)
        # Tokens are written by Codex itself. Do not attempt to read them.
        if os.name != "nt":
            with contextlib.suppress(OSError):
                os.chmod(self.codex_home, stat.S_IRWXU)

    async def start(self) -> asyncio.subprocess.Process:
        async with self._lock:
            if self.healthy:
                return self.process  # type: ignore[return-value]
            self._prepare_home()
            self._stop_requested = False
            command = shlex.split(self.codex_path, posix=os.name != "nt")
            command.append("app-server")
            if self.force_http_transport:
                # Some Windows/proxy paths repeatedly time out the Responses
                # WebSocket before Codex falls back to HTTPS.  This remains an
                # official App Server + ChatGPT-auth flow; only its transport is
                # selected explicitly.
                command.extend(
                    [
                        "-c",
                        "model_provider=astrbot_chatgpt_http",
                        "-c",
                        'model_providers.astrbot_chatgpt_http.name="ChatGPT HTTP"',
                        "-c",
                        'model_providers.astrbot_chatgpt_http.base_url="https://chatgpt.com/backend-api/codex"',
                        "-c",
                        "model_providers.astrbot_chatgpt_http.wire_api=responses",
                        "-c",
                        "model_providers.astrbot_chatgpt_http.requires_openai_auth=true",
                        "-c",
                        "model_providers.astrbot_chatgpt_http.supports_websockets=false",
                    ]
                )
            command.append("--stdio")
            env = os.environ.copy()
            env["CODEX_HOME"] = str(self.codex_home)
            try:
                process = await asyncio.create_subprocess_exec(
                    *command,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env=env,
                )
            except FileNotFoundError as exc:
                self.last_error = "找不到 Codex 可执行文件"
                raise CodexProcessError(
                    "找不到 Codex 可执行文件。请先安装 Codex CLI，或在插件设置的 "
                    "codex_path 中填写 codex 的绝对路径；Transport 推理不启动 App Server，"
                    "但首次 ChatGPT 登录仍需要通过 Codex App Server 完成官方 OAuth。"
                ) from exc
            except (OSError, ValueError) as exc:
                self.last_error = redact_text(str(exc))
                raise CodexProcessError(
                    f"Unable to start Codex app-server: {self.last_error}"
                ) from exc
            self.process = process
            self.last_error = None
            self._stderr_task = asyncio.create_task(self._read_stderr(process), name="codex-stderr")
            self._monitor_task = asyncio.create_task(
                self._monitor(process), name="codex-process-monitor"
            )
            return process

    async def _read_stderr(self, process: asyncio.subprocess.Process) -> None:
        assert process.stderr is not None
        try:
            async for raw in process.stderr:
                line = redact_text(raw.decode(errors="replace").rstrip())
                if line:
                    self.logger.info("[codex app-server] %s", line[:2000])
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.logger.debug("Codex stderr reader stopped: %s", redact_text(str(exc)))

    async def _monitor(self, process: asyncio.subprocess.Process) -> None:
        try:
            self.last_exit_code = await process.wait()
            if self.process is process:
                self.process = None
            if not self._stop_requested:
                self.last_error = f"app-server exited with code {self.last_exit_code}"
                if self._restart_count < self.restart_limit:
                    delay = min(30.0, 2**self._restart_count)
                    self._restart_count += 1
                    self.logger.warning("Codex app-server crashed; restart backoff %.1fs", delay)
                    await asyncio.sleep(delay)
                    if not self._stop_requested:
                        with contextlib.suppress(Exception):
                            await self.start()

        except asyncio.CancelledError:
            raise

    async def stop(self) -> None:
        async with self._lock:
            self._stop_requested = True
            process = self.process
            self.process = None
            for task in (self._stderr_task, self._monitor_task):
                if task and task is not asyncio.current_task():
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await task
            self._stderr_task = None
            self._monitor_task = None
            if process and process.returncode is None:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=5)
                except TimeoutError:
                    process.kill()
                    await process.wait()
            self._restart_count = 0
