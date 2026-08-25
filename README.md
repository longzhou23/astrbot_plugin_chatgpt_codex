# AstrBot ChatGPT Codex Bridge

`astrbot_plugin_chatgpt_codex` lets AstrBot use models made available to the signed-in ChatGPT account through the open-source Codex transport implementation. The default is the stable `codex app-server` backend; an explicitly selected experimental `transport` backend can send direct Responses HTTP/SSE requests without creating Codex threads or turns. It does not use ChatGPT web cookies, browser capture, or a fabricated OpenAI-compatible endpoint.

## First beta release

This repository is published as `v0.3.0-beta.1`, the first public beta of the
current implementation. The recommended production-like path is still the
default `app_server` backend. The direct `transport` backend is included for
testing and comparison, and should be enabled deliberately because its
ChatGPT Codex endpoint shape can change with future Codex client releases.

The beta is intended for a fresh AstrBot test installation. Back up the
plugin data directory before upgrading an existing installation, especially
when switching backend modes or changing the Codex executable.

## Important authorization boundary

ChatGPT Plus and the OpenAI API are separate products with separate authorization and billing. This plugin does not claim that a Plus subscription is an OpenAI API quota or API key. It asks the locally installed Codex App Server to perform its supported ChatGPT login flow, then uses only the account's actual Codex model catalog and rate limits.

The server's `model/list` response is authoritative. Model ids and reasoning efforts are not hard-coded, so names can change or be unavailable for a particular account.

## Backend architecture

AstrBot's stable plugin surface exposes custom Providers, commands, and hooks. The plugin therefore uses a thin `chatgpt_codex` Provider adapter; AstrBot remains the outer Agent Runner and owns persona, memory, history, RAG, MCP, permissions, and tool-loop decisions. `CodexService` selects the inference backend:

```text
AstrBot Agent Runner / message
        |
        v
chatgpt_codex Provider (thin adapter)
        |
        v
CodexService -- backend_mode=app_server --> codex app-server --stdio
        |                                      thread/start + turn/start
        |
        `-- backend_mode=transport -------> Codex Responses HTTP/SSE
                                             no thread/turn/tool harness
```

`backend_mode=app_server` is the default and preserves the previous behavior. `transport` uses the same `CODEX_HOME` login state, `.../codex/models`, and `.../codex/responses` shapes used by the open-source Codex client, but deliberately has no thread/turn, shell, filesystem, MCP, computer, browser, approval, or Codex built-in-tool methods. AstrBot `ToolSet` schemas are converted to Responses function tools and returned as structured tool calls to AstrBot; the plugin does not execute them. `auto` tries transport once and falls back to App Server on auth, model, protocol, network, or rate-limit failure. A fallback is one attempt only; quota exhaustion is never retried indefinitely.

The direct transport is experimental because the ChatGPT Codex backend endpoint is implemented in the open-source Codex client rather than documented as a general public API contract. It may change with a future Codex release. If reliability is more important than the transport experiment, keep `app_server` selected.

## Lightweight Chat Harness

The default `harness_mode` is `lightweight`. With the installed Codex 0.146.0
App Server protocol, `thread/start` and `thread/resume` accept a real
`baseInstructions` replacement field. The plugin uses that field instead of
appending a second prompt, and supplies a short chat-only harness under 100
English words. AstrBot's persona remains a separate `developerInstructions`
value, so changing the persona changes the thread prompt version and rolls the
thread instead of leaving an old persona in place.

Lightweight threads also disable the current optional prompt sources through a
thread-scoped Codex config override: permissions/apps/collaboration/skill and
environment blocks, project docs, memories, MCP servers, Codex apps, the plan
tool, and request-user-input. No AstrBot dynamic tools are sent in the default
`minimal` route. The current App Server schema does not expose a general
"disable every built-in core tool" field; the plugin therefore does not claim
to remove server-owned shell/environment schemas when the server chooses to
register them. It keeps the read-only, no-network sandbox and declines
unexpected approval requests. `harness_mode=codex` leaves the server's native
base instructions/configuration in place for coding-agent use.

The repeatable `scripts/benchmark_prompt_overhead.py` sends only `hi` on
ephemeral threads and reads the real `thread/tokenUsage/updated` totals. It
reports client-declared dynamic-tool bytes separately because the App Server
does not provide a built-in tool-schema listing RPC. Use `--only C` or
`--only D` to isolate the optimized variants, and keep the benchmark on an
isolated Codex App Server process rather than treating local historical Usage
records as A/B data.

## Files

- `main.py`: AstrBot Star, lifecycle, and `/gpt` command group.
- `agent_provider.py`: thin Provider adapter registered as `chatgpt_codex`.
- `codex_service.py`: auth, model catalog, thread mapping, turn streaming, policy controls.
- `codex_rpc.py`: concurrent async JSONL RPC client with pending futures and notifications.
- `process_manager.py`: isolated `CODEX_HOME`, process supervision, stderr logging, restart backoff.
- `session_store.py`: SQLite mapping from AstrBot unified session to Codex thread.
- `model_catalog.py`: server response parsing and non-secret model cache.
- `tool_bridge.py`: disabled extension point for future AstrBot tool schemas.
- `harness.py`: lightweight base-instruction and thread capability policy.
- `transport/`: direct Responses client, OAuth bridge, SSE parser, model/quota adapters, and transport types.
- `scripts/benchmark_prompt_overhead.py`: real App Server A/B overhead benchmark.
- `codex_security.py` / `codex_errors.py`: redaction and error classification.

## Cache and session behavior

The provider uses AstrBot's supplied unified `session_id` as the conversation key. It does not synthesize a random key and it refuses a normal turn without a session id, so a missing identifier cannot silently make all users share `astrbot:default`. The unified AstrBot key is responsible for separating private chats and groups; the plugin persists that key to the Codex thread id in SQLite without storing prompt text.

Within one app-server process, a mapped thread is used directly for later turns. `thread/resume` is sent only when a persisted mapping is first used after process/reconnect, not on every message. A mapping is rolled over when its deterministic prompt version changes, it is idle for the configured TTL, reaches the configured maximum age, reaches `max_thread_turns`, or Codex reports that resume is not possible. Defaults are 7 days idle, 30 days maximum age, and 100 completed turns; all are configurable.

The stable prompt version hashes the normalized developer/system instructions,
selected harness, thread-scoped Codex config, canonical tool schema, and static
local-tools setting. Current user text, attachments, message ids, request ids,
timestamps, latency, and retry state are not put into that hash or developer
prompt. The first turn after a reset may include the required historical
context bootstrap; later turns send only the new user turn because Codex owns
the resumed thread history.

Codex 0.146.0's generated app-server schema exposes `thread/tokenUsage/updated` with `threadId`, `turnId`, and `tokenUsage.last` / `tokenUsage.total` breakdowns. The plugin records only numeric usage fields, latest turn latency, reuse flag, and retry count for diagnostics; it never records the full prompt. `last_usage` and `last_turn` in `/gpt status` are unavailable until the current runtime emits the notification. `cachedInputTokens` is stored as an input breakdown and is never added again to the server-provided `totalTokens`.

## Install and configure

1. Install a current Codex CLI binary on the AstrBot host. `app_server` needs `codex app-server --stdio`; `transport` still uses the same Codex OAuth `CODEX_HOME` but does not start the App Server for inference. Set `codex_path` to an absolute executable path when `codex` is not on `PATH`.
2. Copy this directory into `<AstrBot root>/data/plugins/astrbot_plugin_chatgpt_codex` or install its zip through AstrBot's plugin manager.
3. Restart or reload AstrBot. In the model-provider settings, enable the `ChatGPT Codex Subscription` provider and select it for the target conversation. No OpenAI API key is required by this plugin.
4. Open the installed plugin's `account` page in AstrBot WebUI. Choose browser OAuth or device code, then click `使用 ChatGPT 登录`. For browser OAuth, open the one-time authorization URL. If it does not complete automatically after the browser lands on a `http://localhost:.../auth/callback?...` address (common when the browser is not on the AstrBot host), copy that **entire callback address** from the browser address bar into the page's `提交 localhost 回调` field. The authenticated plugin page forwards it once to the local Codex App Server listener on the AstrBot host. The callback is immediately cleared, is not persisted or logged, and is only accepted for the exact listener port generated by the active login. Polling stops after the account is reported as logged in.
5. Use that same page for status, logout, model refresh, and quota. The `/gpt ...` commands remain administrator-only fallbacks for headless or remote deployments.

The authenticated `account` page is the profile-style overview: account identity, plan, current quota window, reset countdown, quota-activity visualization, server models, and safe runtime status. The separate `settings` tab includes the `backend_mode` selector (`app_server`, `transport`, `auto`) and an optional `transport_proxy` field alongside the Chinese settings form. It reads the current plugin configuration on entry and saves validated settings through the plugin Web API. Codex executable, HTTPS transport, and maximum concurrency changes are marked as requiring an AstrBot restart; backend selection and the explicit Transport proxy apply to the next request. AstrBot clears inherited system proxy variables at startup, so a host behind a local HTTP proxy should set `transport_proxy` explicitly (for example `http://127.0.0.1:7890`).

The quota activity grid is intentionally an aggregate current-window visualization, not fabricated historical daily Token data: the Codex App Server rate-limit API currently exposes rolling windows rather than a contribution-history feed. The account profile accepts a future public HTTPS avatar field when the server provides one, but the current official `account/read` schema only defines account type, email, and plan, so the UI safely falls back to an initial avatar instead of querying ChatGPT web/private endpoints.

The plugin stores its data in `data/plugin_data/astrbot_plugin_chatgpt_codex/`. Codex owns the credential files under that directory's `CODEX_HOME`; the plugin never opens, parses, logs, or copies those files. On Linux, the directory is created with mode `0700` when possible. Keep the AstrBot service account's data directory private and do not put it on a shared volume.

## Commands

WebUI is the primary management surface: AstrBot Dashboard → Plugins → `ChatGPT Codex Subscription` → `account`. Login, logout, status, model refresh, and quota are exposed there through authenticated plugin APIs. The commands below are retained for headless operation and diagnostics.

`/gpt status` shows process health, non-secret account metadata, selected model/effort, and cache count.

`/gpt login` and `/gpt logout` are administrator-only. `login_mode` selects `browser` or `device_code`.

`/gpt models` refreshes and prints the current `model/list` catalog, including each model's advertised reasoning efforts.

`/gpt model <id>` and `/gpt effort <level>` are administrator-only. `auto` leaves the server's default selection in place.

`/gpt harness lightweight|codex` switches the thread-level base-instruction
policy for new or rotated threads. `/gpt prompt-debug` is administrator-only
and returns only lengths, fingerprints, mode, and the last redacted context
diagnostics; it never prints raw persona text, history, credentials, or hidden
reasoning.

`/gpt quota` calls `account/rateLimits/read`. Quota/usage errors are surfaced as a terminal error; the plugin does not retry them indefinitely.

`/gpt benchmark transport` runs one explicit real `hello` request through direct transport and reports latency plus the server-returned usage. `/gpt benchmark app_server` does the same through the existing App Server path. These commands are administrator-only and are never run automatically.

## Usage Tracking

The Usage tab and `/gpt usage` are a local aggregate, deliberately separate from `/gpt quota`:

- Official account limits come from `account/rateLimits/read` and are shown as rate-limit windows and reset times.
- In `transport` mode, direct responses expose only the rate-limit headers returned on the Responses stream; if the service returns none, the UI reports that no direct header snapshot is available rather than inventing an account window. App Server remains the authoritative quota source when `app_server` is selected.
- Direct transport records `response.completed.usage` as a per-request usage record. It does not convert prompt length or context-window size into token estimates.
- Local token usage is collected only after this plugin is installed and a completed Codex turn emits `thread/tokenUsage/updated`. The current protocol fields used are `tokenUsage.last.inputTokens`, `cachedInputTokens`, `outputTokens`, `reasoningOutputTokens`, and the authoritative `totalTokens`, together with the notification's `threadId` and `turnId`.
- `tokenUsage.total` is a cumulative thread/session snapshot; each completed turn is persisted as a field-by-field delta from the previous snapshot. `tokenUsage.last` is the latest active-context snapshot and is used only for context diagnostics. A unique SQLite `turn_id` plus a durable `usage_snapshots` baseline prevents duplicate accounting after reconnects, resume replay, or process restarts.
- Cached input is a subset of input, and reasoning output is a breakdown of output. Neither is added on top of the server-provided `totalTokens`. If one cumulative field moves backwards, that field is treated as a counter reset and the current value starts a new non-negative delta.
- Records are stored at `data/plugin_data/astrbot_plugin_chatgpt_codex/usage.db` with a hashed conversation ID, UTC timestamp, configured local date, model, selected effort, numeric token deltas, context size, and request count. Prompts, responses, credentials, cookies, and raw events are not stored. Existing v1 records are moved to `usage_records_legacy_v1` during schema migration and are not silently mixed into the corrected totals.
- Reasoning counts remain `Unavailable` when the server does not provide `reasoningOutputTokens`; the plugin never estimates tokens from text, context windows, or rate-limit percentages.

The current installed Codex executable exposes the `GetAccountTokenUsageResponse` schema in generated protocol output, but does not expose a callable account-token-usage request/params entry. Therefore the dashboard does not fabricate historical account usage: its daily heatmap is based on the locally observed turn events. A future Codex release can add a separate official history adapter without changing the local schema.

Settings include `usage_timezone` (default `Asia/Shanghai`), `usage_retention_days` (default `365`, or `0` for forever), and `usage_debug` (default `false`). Heatmap levels are adaptive P20/P40/P60/P80 levels over the visible date range; tooltips retain exact values. The overview also shows recent per-turn numeric usage and context-window diagnostics without exposing identifiers, prompts, responses, or hidden reasoning.

`/gpt usage debug` is an administrator-only redacted diagnostic command. It reports the accounting source, snapshot/delta semantics, counter-reset flags, schema version, and recent numeric events. `/gpt usage reset` clears corrected v2 records, baselines, and diagnostics while preserving the legacy v1 table for audit.

`/gpt reset` removes the current AstrBot-to-Codex thread mapping. The next message starts a fresh Codex thread.

## Security defaults

The default configuration has `backend_mode=app_server`, `harness_mode=lightweight`, `tool_router=minimal`, and `enable_local_codex_tools=false`. App Server threads use a read-only sandbox, no sandbox network access, and declined unexpected approvals. Direct transport has no Codex local capability surface at all; only the AstrBot-selected function schemas are sent, and execution remains with AstrBot. Optional Codex prompt sources and MCP/apps are disabled by the lightweight thread config. Raw reasoning events, raw command text, file diffs, MCP payloads, and internal state are not rendered or written to logs.

Only public assistant-message deltas and, when explicitly enabled, generic status labels such as `[fileChange started]` are exposed. A status label never includes a command, path, tool argument, result, or hidden reasoning.

## Validation

From this plugin directory:

```text
python -m pytest
python -m compileall -q .
ruff check .
```

The tests are protocol-level tests and do not require a Codex binary or a live ChatGPT login. A live smoke test should be performed on the target AstrBot host after installing a current Codex binary: `/gpt login` -> `/gpt status` -> `/gpt models` -> one short message -> `/gpt quota`.

## Known limitations and next steps

- App Server mode still buffers Codex text until `item/completed`/`turn/completed` and then emits one authoritative answer. Transport mode parses Responses SSE deltas and emits them progressively; its terminal `LLMResponse` is intentionally empty so the answer is not rendered twice.
- AstrBot image/audio context is not yet converted to the App Server's inline/local input variants; text is the reliable MVP path.
- Transport mode forwards AstrBot function schemas and returns tool calls to AstrBot's Agent Runner, but the current MVP does not execute a transport-side multi-call loop itself. App Server mode continues to keep the Codex loop isolated and does not receive AstrBot tools.
- Codex executable availability, ChatGPT plan entitlements, regional access, quota behavior, and protocol details are external runtime dependencies. The model catalog and errors must be checked on the target machine.
- Provider selection still uses AstrBot's Provider registry because that is the stable plugin integration point. Transport mode bypasses Codex Agent Harness; App Server mode retains it for compatibility.
