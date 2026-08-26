# AstrBot ChatGPT Codex Bridge

[中文文档](README.zh-CN.md)

`astrbot_plugin_chatgpt_codex` lets AstrBot use models made available to the signed-in ChatGPT account through the open-source Codex transport implementation. The recommended default is the lightweight experimental `transport` backend, which sends direct Responses HTTP/SSE requests without creating Codex threads or turns; the stable `codex app-server` backend remains available as a compatibility fallback. It does not use ChatGPT web cookies, browser capture, or a fabricated OpenAI-compatible endpoint.

## First beta release

This repository is published as `v0.3.0-beta.1`, the first public beta of the
current implementation. The recommended default path is the lightweight
`transport` backend. The stable `app_server` backend remains available as a
compatibility fallback when the experimental Responses endpoint is unavailable.
Transport is still an experimental Codex client protocol surface and its
ChatGPT endpoint shape can change with future Codex client releases.

The beta is intended for a fresh AstrBot test installation. Back up the
plugin data directory before upgrading an existing installation, especially
when switching backend modes or changing the Codex executable.

## First-start guide for a new instance

The first login has one extra prerequisite that is easy to miss:

### 1. Install Codex CLI

Follow the [official OpenAI Codex CLI setup guide](https://learn.chatgpt.com/docs/codex/cli)
for the current installer. The commonly used commands are:

**macOS / Linux (standalone installer):**

```bash
curl -fsSL https://chatgpt.com/codex/install.sh | sh
```

**Windows, macOS, or Linux (npm):**

```bash
npm install -g @openai/codex
```

The npm route requires a supported Node.js/npm installation. After installation,
open a new terminal if the command is not immediately visible and verify it:

```text
codex --version
```

Run this check as the same operating-system user that runs AstrBot. If the
service account cannot find `codex` on `PATH`, set `codex_path` in the plugin
settings to the absolute executable path, for example
`C:\\Users\\<user>\\AppData\\Roaming\\npm\\codex.cmd` on Windows or
`/usr/local/bin/codex` on Linux/macOS. The plugin uses the official Codex App
Server OAuth / Device Code RPC for the first ChatGPT login.

On the first visit to the overview page, the plugin shows a welcome setup panel.
It probes `codex` in the PATH of the environment where AstrBot is actually running.
If the probe succeeds, leave the value as `codex`; only enter an absolute path when
the probe fails. The same panel lets you choose browser OAuth or Device Code and
starts the first login after saving the settings.

#### Docker users

When AstrBot runs in Docker, the plugin can see only the container filesystem and
PATH. A host path such as `/usr/bin/codex`, `/usr/lib/node_modules`, or a Windows
path is not visible inside the container. Check the container that actually runs
AstrBot:

```bash
docker compose exec astrbot sh
command -v codex
codex --version
```

If Codex is installed in the container but is not on `PATH`, enter the absolute path
reported by `command -v codex` in the welcome page or settings page. For a durable
deployment, put the installation in your Dockerfile and rebuild the image instead of
installing into a running container:

```dockerfile
RUN npm install -g --include=optional @openai/codex@0.149.1
```

The plugin stores `CODEX_HOME` below AstrBot's persistent data directory, so the
Compose deployment must mount `/AstrBot/data`. Complete ChatGPT OAuth once in the
plugin welcome page; later restarts reuse the persisted login state. Do not copy a
host Codex path into the container configuration.

### 2. Complete the first login

1. Restart AstrBot and open the plugin's WebUI overview. Click the ChatGPT
   login button and finish the browser OAuth or Device Code flow. The login
   button uses the mode currently saved in the welcome/settings page; Device
   Code displays its verification URL and one-time user code.
2. If browser OAuth ends at a `localhost` callback URL, paste the complete URL
   back into the plugin page, including the full `?code=...&state=...` query.
   Do not paste only the path or code and do not share the URL in logs or issue
   reports. On a remote server, the browser's localhost is the browser computer,
   not the server, so the complete callback must be submitted manually.
3. After the page reports that the account is logged in, the credentials are
   persisted in the plugin-owned `CODEX_HOME`. The recommended `transport`
   backend can then read the login state and perform Responses inference
   without starting `codex app-server` for each request.

If a new instance shows `No such file or directory: 'codex'` or
`Unable to start Codex app-server`, Codex CLI is missing or `codex_path` is
wrong. Fix that prerequisite first. Selecting `app_server` also requires the
Codex executable for inference; selecting `transport` removes the App Server
runtime requirement only after the initial official login has completed.

The plugin never asks users to copy ChatGPT cookies, access tokens, refresh
tokens, or passwords. Do not manually edit or share `CODEX_HOME/auth.json`.

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
CodexService -- backend_mode=transport -------> Codex Responses HTTP/SSE
        |                                        no thread/turn/tool harness
        |
        `-- backend_mode=app_server -------> codex app-server (stdio default)
                                             thread/start + turn/start
```

`backend_mode=transport` is the new default and recommended path. It uses the same `CODEX_HOME` login state, `.../codex/models`, and `.../codex/responses` shapes used by the open-source Codex client, but deliberately has no thread/turn, shell, filesystem, MCP, computer, browser, approval, or Codex built-in-tool methods. AstrBot `ToolSet` schemas are converted to Responses function tools and returned as structured tool calls to AstrBot; the plugin does not execute them. `backend_mode=app_server` remains the stable compatibility fallback. `auto` tries transport once and falls back to App Server on auth, model, protocol, network, or rate-limit failure. A fallback is one attempt only; quota exhaustion is never retried indefinitely.

The direct transport is experimental because the ChatGPT Codex backend endpoint is implemented in the open-source Codex client rather than documented as a general public API contract. It may change with a future Codex release. If a deployment needs the stable Agent Server protocol, select `app_server` explicitly.

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

The provider uses AstrBot's supplied unified `session_id` as the conversation key. Normal chat turns never use a shared fallback. AstrBot's `Context.llm_generate()` helper does not always provide a session id for plugin-owned background calls, so those calls receive a unique ephemeral key and their local mapping is cleaned up after the request; they cannot share a normal user conversation. The unified AstrBot key is responsible for separating private chats and groups; the plugin persists that key to the Codex thread id in SQLite without storing prompt text.

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

1. Install a current Codex CLI binary on the AstrBot host. The recommended `transport` backend uses the same Codex OAuth `CODEX_HOME` and does not launch App Server for inference, but the first ChatGPT OAuth/Device Code login still uses the official `codex app-server` login RPC. `app_server` starts `codex app-server` with the current default stdio transport; the plugin deliberately does not pass the removed legacy `--stdio` flag. Set `codex_path` to an absolute executable path when `codex` is not on `PATH`.
2. Copy this directory into `<AstrBot root>/data/plugins/astrbot_plugin_chatgpt_codex` or install its zip through AstrBot's plugin manager.
3. Restart or reload AstrBot. In the model-provider settings, enable the `ChatGPT Codex Subscription` provider and select it for the target conversation. No OpenAI API key is required by this plugin.
4. Open the installed plugin's `account` page in AstrBot WebUI. Choose browser OAuth or device code, then click `使用 ChatGPT 登录`. For browser OAuth, open the one-time authorization URL. If it does not complete automatically after the browser lands on a `http://localhost:.../auth/callback?...` address (common when the browser is not on the AstrBot host), copy that **entire callback address** from the browser address bar into the page's `提交 localhost 回调` field. The authenticated plugin page forwards it once to the local Codex App Server listener on the AstrBot host. The callback is immediately cleared, is not persisted or logged, and is only accepted for the exact listener port generated by the active login. Polling stops after the account is reported as logged in.
5. Use that same page for status, logout, model refresh, and quota. The `/gpt ...` commands remain administrator-only fallbacks for headless or remote deployments.

The authenticated `account` page is the profile-style overview: account identity, plan, current quota window, reset countdown, quota-activity visualization, server models, and safe runtime status. The separate `settings` tab includes the `backend_mode` selector (`transport` recommended, `app_server` stable fallback, `auto`), the `use_system_proxy` switch, and an optional explicit `transport_proxy` field alongside the Chinese settings form. It reads the current plugin configuration on entry and saves validated settings through the plugin Web API. System proxy variables are inherited by Transport and App Server login; an explicit proxy overrides them. In Docker, pass `HTTP_PROXY`, `HTTPS_PROXY`, and `ALL_PROXY` into the container, because the host desktop proxy is not automatically visible inside it.

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

The default configuration has `backend_mode=transport`, `harness_mode=lightweight`, `tool_router=minimal`, and `enable_local_codex_tools=false`. Direct transport has no Codex local capability surface at all; only the AstrBot-selected function schemas are sent, and execution remains with AstrBot. When `app_server` is selected, threads use a read-only sandbox, no sandbox network access, and declined unexpected approvals. Optional Codex prompt sources and MCP/apps are disabled by the lightweight thread config. Raw reasoning events, raw command text, file diffs, MCP payloads, and internal state are not rendered or written to logs.

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

- App Server mode still buffers Codex text until `item/completed`/`turn/completed` and then emits one authoritative answer. Transport mode parses Responses SSE deltas and emits them progressively, then emits one completed terminal response for AstrBot's Agent Runner without rendering the answer a second time.
- AstrBot image/audio context is not yet converted to the App Server's inline/local input variants; text is the reliable MVP path.
- Transport mode forwards AstrBot function schemas and returns tool calls to AstrBot's Agent Runner; tool results, structured function-call history, image inputs, and opaque Responses reasoning state are preserved across the next request. The plugin still does not execute a second transport-side Agent Loop. App Server mode continues to keep the Codex loop isolated and does not receive AstrBot tools.
- Codex executable availability, ChatGPT plan entitlements, regional access, quota behavior, and protocol details are external runtime dependencies. The model catalog and errors must be checked on the target machine.
- Provider selection still uses AstrBot's Provider registry because that is the stable plugin integration point. Transport mode bypasses Codex Agent Harness; App Server mode retains it for compatibility.
