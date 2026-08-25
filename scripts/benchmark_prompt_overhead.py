"""Measure Codex App Server prompt overhead without AstrBot history.

This benchmark intentionally uses isolated threads and sends only ``hi``. It
does not read or print credentials. The returned token values come directly
from ``thread/tokenUsage/updated`` and are not estimated by the plugin.

Examples::

    python scripts/benchmark_prompt_overhead.py \
        --codex-path codex \
        --codex-home data/plugin_data/astrbot_plugin_chatgpt_codex/CODEX_HOME

The benchmark variants are deliberately explicit:

* A: app-server defaults, no client-declared dynamic tools;
* B: default base instructions plus conservative no-app/no-extra-tool config;
* C: the short lightweight base instructions plus the same config;
* D: lightweight instructions, no environment context, and an empty cwd.

Codex's built-in tool registry is server-side and is not exposed as a schema
listing RPC. The report therefore distinguishes client-declared dynamic-tool
bytes from that unobservable server-side portion instead of inventing a count.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import shlex
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

LIGHTWEIGHT_BASE_INSTRUCTIONS = (
    "You are the reasoning backend for an AstrBot agent. "
    "Follow the supplied persona and system instructions. "
    "Respond to the current user directly and concisely. "
    "Use only tools explicitly provided for this turn; do not assume access "
    "to a shell, filesystem, browser, repository, or local machine. "
    "Never expose hidden reasoning or internal state."
)


@dataclass(frozen=True)
class Variant:
    name: str
    base_instructions: str | None = None
    config: dict[str, Any] | None = None
    include_cwd: bool = True


def no_extra_tools_config(*, include_environment: bool = False) -> dict[str, Any]:
    """Return only settings supported by the current Codex config schema."""

    return {
        "include_permissions_instructions": False,
        "include_apps_instructions": False,
        "include_collaboration_mode_instructions": False,
        "include_skill_instructions": False,
        "include_environment_context": include_environment,
        "project_doc_max_bytes": 0,
        "use_memories": False,
        "mcp_servers": {},
        "apps": {
            "_default": {
                "enabled": False,
                "default_tools_enabled": False,
                "open_world_enabled": False,
                "destructive_enabled": False,
            }
        },
        "tools": {
            "update_plan": {"enabled": False},
            "experimental_request_user_input": {"enabled": False},
        },
    }


def variants(empty_cwd: str) -> list[Variant]:
    return [
        Variant("A Default"),
        Variant("B No Local Tools", config=no_extra_tools_config(include_environment=False)),
        Variant(
            "C Lightweight",
            base_instructions=LIGHTWEIGHT_BASE_INSTRUCTIONS,
            config=no_extra_tools_config(include_environment=False),
        ),
        Variant(
            "D Lightweight + Empty Environment",
            base_instructions=LIGHTWEIGHT_BASE_INSTRUCTIONS,
            config=no_extra_tools_config(include_environment=False),
            include_cwd=False,
        ),
    ]


class Rpc:
    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.reader = reader
        self.writer = writer
        self.next_id = 0
        self.pending: dict[int, asyncio.Future[Any]] = {}
        self.events: asyncio.Queue[tuple[str, dict[str, Any]]] = asyncio.Queue()
        self.reader_task = asyncio.create_task(self._read_loop())

    async def _read_loop(self) -> None:
        try:
            while line := await self.reader.readline():
                message = json.loads(line)
                if not isinstance(message, dict):
                    continue
                if "id" in message and ("result" in message or "error" in message):
                    future = self.pending.get(message.get("id"))
                    if future is None or future.done():
                        continue
                    if "error" in message:
                        error = message.get("error") or {}
                        future.set_exception(
                            RuntimeError(str(error.get("message", "Codex RPC error")))
                        )
                    else:
                        future.set_result(message.get("result"))
                    continue
                method = message.get("method")
                if not isinstance(method, str):
                    continue
                params = message.get("params")
                if not isinstance(params, dict):
                    params = {}
                if "id" in message:
                    # Benchmark turns are intentionally non-interactive and
                    # must never approve a local capability request.
                    await self._send(
                        {"jsonrpc": "2.0", "id": message["id"], "result": {"decision": "decline"}}
                    )
                else:
                    await self.events.put((method, params))
            raise RuntimeError("Codex app-server closed stdout")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            for future in tuple(self.pending.values()):
                if not future.done():
                    future.set_exception(exc)

    async def _send(self, message: dict[str, Any]) -> None:
        self.writer.write((json.dumps(message, separators=(",", ":")) + "\n").encode())
        # The benchmark frames are tiny.  Avoid awaiting StreamWriter.drain()
        # on Windows anonymous pipes: a stalled app-server can otherwise keep
        # the Proactor loop inside the drain syscall and defeat the timeout.
        await asyncio.sleep(0)

    async def request(self, method: str, params: dict[str, Any], timeout: float = 180) -> Any:
        self.next_id += 1
        request_id = self.next_id
        future = asyncio.get_running_loop().create_future()
        self.pending[request_id] = future
        try:
            # Cover both pipe backpressure and a server-side request that
            # never produces a response.  On Windows a blocked stdin drain
            # otherwise makes a benchmark appear to hang forever.
            async def send_and_wait() -> Any:
                await self._send(
                    {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
                )
                return await future

            return await asyncio.wait_for(send_and_wait(), timeout)
        finally:
            self.pending.pop(request_id, None)

    async def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        await self._send({"jsonrpc": "2.0", "method": method, "params": params or {}})

    async def close(self) -> None:
        self.reader_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self.reader_task
        self.writer.close()
        with contextlib.suppress(Exception):
            await self.writer.wait_closed()


def server_command(codex_path: str, *, http_transport: bool) -> list[str]:
    executable = Path(codex_path).expanduser()
    if os.name == "nt" and executable.is_file():
        command = [str(executable)]
    else:
        command = shlex.split(codex_path, posix=os.name != "nt")
    command.extend(["app-server"])
    if http_transport:
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
    command.extend(["--stdio"])
    return command


async def run_variant(
    rpc: Rpc,
    variant: Variant,
    *,
    model: str,
    effort: str,
    cwd: str,
    request_timeout: float,
) -> dict[str, Any]:
    params: dict[str, Any] = {
        "approvalPolicy": "on-request",
        "sandbox": "read-only",
    }
    if model != "auto":
        params["model"] = model
    if variant.base_instructions is not None:
        params["baseInstructions"] = variant.base_instructions
    if variant.config is not None:
        params["config"] = variant.config
    if variant.include_cwd:
        params["cwd"] = cwd

    try:
        started = await rpc.request("thread/start", params, timeout=request_timeout)
    except Exception as exc:
        raise RuntimeError(f"{variant.name}: thread/start failed: {exc}") from exc
    thread = started.get("thread") if isinstance(started, dict) else None
    thread_id = thread.get("id") if isinstance(thread, dict) else None
    if not isinstance(thread_id, str):
        raise TypeError(f"{variant.name}: thread/start returned no thread id")

    turn_params: dict[str, Any] = {
        "threadId": thread_id,
        "clientUserMessageId": f"benchmark-{time.time_ns()}",
        "input": [{"type": "text", "text": "hi"}],
        "sandboxPolicy": {"type": "readOnly", "networkAccess": False},
    }
    if model != "auto":
        turn_params["model"] = model
    if effort != "auto":
        turn_params["effort"] = effort
    if variant.include_cwd:
        turn_params["cwd"] = cwd
    try:
        turn = await rpc.request("turn/start", turn_params, timeout=request_timeout)
    except Exception as exc:
        raise RuntimeError(f"{variant.name}: turn/start failed: {exc}") from exc
    turn_info = turn.get("turn") if isinstance(turn, dict) else None
    turn_id = turn_info.get("id") if isinstance(turn_info, dict) else None
    if not isinstance(turn_id, str):
        raise TypeError(f"{variant.name}: turn/start returned no turn id")

    usage: dict[str, Any] | None = None
    completed = False
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline and not completed:
        method, event = await asyncio.wait_for(rpc.events.get(), max(1, deadline - time.monotonic()))
        if event.get("turnId") != turn_id:
            continue
        if method == "thread/tokenUsage/updated":
            token_usage = event.get("tokenUsage")
            if isinstance(token_usage, dict):
                usage = token_usage.get("total") if isinstance(token_usage.get("total"), dict) else None
        elif method == "turn/completed":
            completed = True

    if not completed:
        raise TimeoutError(f"{variant.name}: turn did not complete")
    if usage is None:
        # The usage notification may be delivered immediately after the
        # terminal event. Give it a short bounded grace period.
        with contextlib.suppress(asyncio.TimeoutError):
            while True:
                method, event = await asyncio.wait_for(rpc.events.get(), 2)
                if event.get("turnId") != turn_id:
                    continue
                if method == "thread/tokenUsage/updated":
                    token_usage = event.get("tokenUsage")
                    if isinstance(token_usage, dict) and isinstance(token_usage.get("total"), dict):
                        usage = token_usage["total"]
                        break
    if usage is None:
        raise RuntimeError(f"{variant.name}: no token usage notification")

    return {
        "variant": variant.name,
        "thread_id": thread_id,
        "turn_id": turn_id,
        "input_tokens": usage.get("inputTokens"),
        "cached_input_tokens": usage.get("cachedInputTokens"),
        "output_tokens": usage.get("outputTokens"),
        "reasoning_tokens": usage.get("reasoningOutputTokens"),
        "total_tokens": usage.get("totalTokens"),
        "model_context_window": usage.get("modelContextWindow"),
        "dynamic_tool_schema_bytes": 0,
        "server_builtin_tool_schema": "not exposed by app-server RPC",
    }


async def benchmark(args: argparse.Namespace) -> list[dict[str, Any]]:
    codex_home = Path(args.codex_home).expanduser().resolve()
    codex_home.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="astrbot-codex-benchmark-") as temp_dir:
        command = server_command(args.codex_path, http_transport=not args.no_http_transport)
        env = os.environ.copy()
        env["CODEX_HOME"] = str(codex_home)
        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=temp_dir,
            env=env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        assert process.stdin is not None and process.stdout is not None
        rpc = Rpc(process.stdout, process.stdin)
        try:
            await rpc.request(
                "initialize",
                {
                    "clientInfo": {
                        "name": "astrbot_prompt_overhead_benchmark",
                        "title": "AstrBot Prompt Overhead Benchmark",
                        "version": "0.1.0",
                    },
                    "capabilities": {
                        "experimentalApi": False,
                        "optOutNotificationMethods": [
                            "item/reasoning/summaryTextDelta",
                            "item/reasoning/summaryPartAdded",
                            "item/reasoning/textDelta",
                            "item/plan/delta",
                        ],
                    },
                }
            )
            await rpc.notify("initialized")
            results = []
            selected_variants = variants(temp_dir)
            if args.only:
                selected_variants = [
                    variant
                    for variant in selected_variants
                    if variant.name.startswith(tuple(args.only))
                ]
            for variant in selected_variants:
                print(f"Running {variant.name}...", flush=True)
                results.append(
                    await run_variant(
                        rpc,
                        variant,
                        model=args.model,
                        effort=args.effort,
                        cwd=temp_dir,
                        request_timeout=args.request_timeout,
                    )
                )
            return results
        finally:
            await rpc.close()
            if process.returncode is None:
                process.terminate()
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(process.wait(), 5)
                if process.returncode is None:
                    process.kill()
                    await process.wait()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codex-path", default="codex")
    parser.add_argument("--codex-home", default=os.environ.get("CODEX_HOME", "CODEX_HOME"))
    parser.add_argument("--model", default="auto")
    parser.add_argument("--effort", default="low")
    parser.add_argument("--no-http-transport", action="store_true")
    parser.add_argument(
        "--request-timeout",
        type=float,
        default=60.0,
        help="Per-RPC timeout in seconds (default: 60).",
    )
    parser.add_argument(
        "--only",
        action="append",
        choices=("A", "B", "C", "D"),
        help="Run only selected variants; repeat the option for multiple variants.",
    )
    args = parser.parse_args()
    try:
        results = asyncio.run(benchmark(args))
    except Exception as exc:
        print(f"Benchmark failed: {type(exc).__name__}: {exc}")
        return 1

    print("Codex Prompt Overhead Benchmark")
    print(json.dumps({"results": results}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
