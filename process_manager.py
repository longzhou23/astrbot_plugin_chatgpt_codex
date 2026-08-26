from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import shlex
import shutil
import stat
from pathlib import Path
from urllib.parse import urlsplit

from .codex_errors import CodexProcessError
from .codex_security import redact_text


class CodexProcessManager:
    """Own an isolated long-lived ``codex app-server`` stdio process."""

    def __init__(
        self,
        codex_path: str,
        codex_home: Path,
        *,
        logger: logging.Logger | None = None,
        restart_limit: int = 5,
        force_http_transport: bool = True,
        proxy_url: str = "",
        use_system_proxy: bool = True,
    ) -> None:
        self.codex_path = codex_path or "codex"
        self.codex_home = codex_home
        self.logger = logger or logging.getLogger(__name__)
        self.restart_limit = restart_limit
        self.force_http_transport = force_http_transport
        self.use_system_proxy = bool(use_system_proxy)
        self.proxy_url = ""
        self.set_proxy(proxy_url)
        self.process: asyncio.subprocess.Process | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._monitor_task: asyncio.Task[None] | None = None
        self._stop_requested = False
        self._lock = asyncio.Lock()
        self._restart_count = 0
        self.last_exit_code: int | None = None
        self.last_error: str | None = None

    @staticmethod
    def _validated_proxy(value: str | None) -> str:
        proxy = str(value or "").strip()
        if not proxy:
            return ""
        parsed = urlsplit(proxy)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("网络代理必须是 http:// 或 https:// 地址。")
        if parsed.username or parsed.password:
            raise ValueError("网络代理地址不能包含用户名或密码。")
        if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
            raise ValueError("网络代理只支持主机和端口，不支持路径或查询参数。")
        return proxy

    def set_proxy(self, proxy_url: str | None) -> None:
        """Set an explicit proxy used by the App Server login process.

        The value is validated but never logged or returned by diagnostics.
        A running process must be stopped by the owner before the new value can
        affect its environment.
        """

        self.proxy_url = self._validated_proxy(proxy_url)

    def set_use_system_proxy(self, enabled: bool) -> None:
        self.use_system_proxy = bool(enabled)

    def _subprocess_environment(self) -> dict[str, str]:
        env = os.environ.copy()
        env["CODEX_HOME"] = str(self.codex_home)
        proxy_names = (
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "ALL_PROXY",
            "http_proxy",
            "https_proxy",
            "all_proxy",
        )
        if self.proxy_url:
            for name in proxy_names:
                env[name] = self.proxy_url
            env["NO_PROXY"] = "localhost,127.0.0.1,::1"
            env["no_proxy"] = env["NO_PROXY"]
        elif self.use_system_proxy:
            env.setdefault("NO_PROXY", "localhost,127.0.0.1,::1")
            env.setdefault("no_proxy", env["NO_PROXY"])
        else:
            # AstrBot normally clears inherited proxies.  Remove any leftovers
            # here as well so login routing is controlled only by this setting.
            for name in proxy_names:
                env.pop(name, None)
        return env

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

    def diagnostic(self) -> dict[str, str | bool]:
        """Return non-secret executable discovery information for the WebUI."""

        configured = self.codex_path or "codex"
        try:
            command = shlex.split(configured, posix=os.name != "nt")
        except ValueError as exc:
            return {
                "configured": configured,
                "available": False,
                "error": f"路径格式无效：{redact_text(str(exc))}",
            }
        if not command:
            command = ["codex"]

        executable = command[0]
        resolved = shutil.which(executable)
        if resolved is None and (os.path.isabs(executable) or os.path.dirname(executable)):
            candidate = Path(executable).expanduser()
            if candidate.is_file() and os.access(candidate, os.X_OK):
                resolved = str(candidate)
        available = resolved is not None
        result: dict[str, str | bool] = {
            "configured": configured,
            "available": available,
            "proxyConfigured": bool(self.proxy_url) or self._system_proxy_configured(),
            "explicitProxyConfigured": bool(self.proxy_url),
            "systemProxyEnabled": self.use_system_proxy,
            "environment": "docker"
            if Path("/.dockerenv").exists() or Path("/AstrBot/data").is_dir()
            else "host",
        }
        if resolved:
            result["resolved"] = resolved
        if not available:
            result["error"] = (
                "未找到 Codex 可执行文件。普通安装请确保 codex 在 PATH 中；"
                "Docker 请按 README 的容器内路径指引配置，不要填写宿主机路径。"
            )
        return result

    @staticmethod
    def _system_proxy_configured() -> bool:
        return any(
            bool(os.environ.get(name))
            for name in (
                "HTTP_PROXY",
                "HTTPS_PROXY",
                "ALL_PROXY",
                "http_proxy",
                "https_proxy",
                "all_proxy",
            )
        )

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
            # Current Codex App Server uses stdio by default.  Older versions
            # exposed a ``--stdio`` spelling, but recent CLI releases reject
            # that flag and exit immediately, which surfaces to the RPC client
            # as "app-server closed stdout".  Do not pass a transport flag;
            # pipes supplied by create_subprocess_exec select the default stdio
            # transport and remain compatible with the current protocol.
            env = self._subprocess_environment()
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
