# AstrBot ChatGPT Codex Bridge

`astrbot_plugin_chatgpt_codex` lets AstrBot use models made available to the signed-in ChatGPT account through the official `codex app-server` protocol. It does not use ChatGPT web cookies, private BFF endpoints, browser capture, or a fabricated OpenAI-compatible endpoint.

## Important authorization boundary

ChatGPT Plus and the OpenAI API are separate products with separate authorization and billing. This plugin does not claim that a Plus subscription is an OpenAI API quota or API key. It asks the locally installed Codex App Server to perform its supported ChatGPT login flow, then uses only the account's actual Codex model catalog and rate limits.

The server's `model/list` response is authoritative. Model ids and reasoning efforts are not hard-coded, so names can change or be unavailable for a particular account.

## MVP architecture

AstrBot's stable plugin surface exposes custom Providers, commands, and hooks. At the time of implementation it does not expose a stable Star-side registration contract for a third-party Agent Runner. The MVP therefore uses a deliberately thin `chatgpt_codex` Provider adapter, while the actual orchestration is `CodexService`:

```text
AstrBot message / Agent request
        |
        v
chatgpt_codex Provider (thin adapter)
        |
        v
CodexService -> CodexProcessManager -> codex app-server --stdio
        |                         |
        |                         +-- account/login, model/list, quota
        +-- AstrBot session -> Codex thread/resume -> turn/start
```

Codex owns its own thread/turn Agent Loop. AstrBot's normal tool loop is not run again for this Provider, and AstrBot tools are not passed to Codex in the MVP. `tool_bridge.py` is the reserved boundary for a future `dynamicTools` bridge; it is disabled by default to avoid a double Agent Loop and capability escalation.

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

1. Install a current Codex CLI binary on the AstrBot host and make sure `codex app-server --stdio` works for the AstrBot service account. Set `codex_path` to an absolute executable path when `codex` is not on `PATH`.
2. Copy this directory into `<AstrBot root>/data/plugins/astrbot_plugin_chatgpt_codex` or install its zip through AstrBot's plugin manager.
3. Restart or reload AstrBot. In the model-provider settings, enable the `ChatGPT Codex Subscription` provider and select it for the target conversation. No OpenAI API key is required by this plugin.
4. Open the installed plugin's `account` page in AstrBot WebUI. Choose browser OAuth or device code, then click `使用 ChatGPT 登录`. For browser OAuth, open the one-time authorization URL. If it does not complete automatically after the browser lands on a `http://localhost:.../auth/callback?...` address (common when the browser is not on the AstrBot host), copy that **entire callback address** from the browser address bar into the page's `提交 localhost 回调` field. The authenticated plugin page forwards it once to the local Codex App Server listener on the AstrBot host. The callback is immediately cleared, is not persisted or logged, and is only accepted for the exact listener port generated by the active login. Polling stops after the account is reported as logged in.
5. Use that same page for status, logout, model refresh, and quota. The `/gpt ...` commands remain administrator-only fallbacks for headless or remote deployments.

The authenticated `account` page is the profile-style overview: account identity, plan, current quota window, reset countdown, quota-activity visualization, server models, and safe runtime status. Because AstrBot embeds plugin pages in a sandboxed iframe, the account page also contains a real local `设置` tab; it reads the current plugin configuration on entry and saves validated settings through the plugin Web API. The standalone `settings` route remains available for direct opening and contains the same Chinese settings form. The form covers the Codex executable, login mode, server-selected model and reasoning effort, harness mode, tool router, concurrency/timeouts, thread rollover, streaming, safe status labels, HTTPS transport, and the local-tools security switch. Codex executable, HTTPS transport, and maximum concurrency changes are marked as requiring an AstrBot restart; lightweight harness and router changes apply to the next thread.

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

## Usage Tracking

The Usage tab and `/gpt usage` are a local aggregate, deliberately separate from `/gpt quota`:

- Official account limits come from `account/rateLimits/read` and are shown as rate-limit windows and reset times.
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

The default configuration has `harness_mode=lightweight`, `tool_router=minimal`, and `enable_local_codex_tools=false`. The plugin starts threads with a read-only sandbox, no sandbox network access, and an approval policy that causes unexpected approval requests to be declined. It does not pass AstrBot's local shell, filesystem-write, MCP, computer-control, browser-control, or function tools to Codex. Optional Codex prompt sources and MCP/apps are disabled by the lightweight thread config. Built-in core tool registration remains controlled by the installed App Server because its current protocol has no global tool-disable parameter; the plugin reports this limitation instead of pretending that a prompt instruction removed those schemas. Raw reasoning events, raw command text, file diffs, MCP payloads, and internal state are not rendered or written to logs.

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

- Streaming mode currently buffers Codex text until `item/completed`/`turn/completed` and then emits one authoritative final chunk. This intentionally avoids duplicate or contradictory text when the upstream response stream reconnects; safe progressive delta reconciliation is planned for a later release.
- The AstrBot adapter follows that visible chunk with one non-chunk terminal `LLMResponse`. The terminal frame is required for AstrBot's Agent Runner to finish the step and is not rendered a second time.
- AstrBot image/audio context is not yet converted to the App Server's inline/local input variants; text is the reliable MVP path.
- The MVP does not bridge AstrBot tools into Codex `dynamicTools`; doing so needs explicit user-visible approval semantics, a tool-call request handler, and tests against the exact current experimental protocol.
- Codex executable availability, ChatGPT plan entitlements, regional access, quota behavior, and protocol details are external runtime dependencies. The model catalog and errors must be checked on the target machine.
- Provider selection still uses AstrBot's Provider registry because that is the stable plugin integration point. It delegates Agent orchestration to Codex rather than emulating an OpenAI Chat API.
