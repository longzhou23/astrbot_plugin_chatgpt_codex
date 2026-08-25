# Changelog

## 0.3.0-beta.1 — 2026-08-25

First public beta release of `astrbot_plugin_chatgpt_codex`.

### Included

- Stable `codex app-server` integration as the default backend.
- Experimental direct Codex Responses HTTP/SSE transport for lightweight chat
  requests that do not create Codex threads or turns.
- ChatGPT OAuth and device-code login through the Codex-supported flow, with
  an isolated persistent `CODEX_HOME` and redacted logs.
- Dynamic account, quota, model, and reasoning-effort discovery from the
  server; model ids are not hard-coded.
- AstrBot Provider integration with per-session Codex thread mapping,
  serialized turns per session, concurrent sessions, reset support, and
  streaming text output.
- Usage accounting based on real response usage, including input, cached
  input, output, total, cache-hit tokens, cache-hit rate, and recent-turn
  details.
- Overview/settings WebUI tabs with AstrBot-style black, white, and blue
  visuals, light/dark mode support, OAuth callback handoff, and safe default
  tool restrictions.
- Async JSONL RPC and SSE parsing tests, usage regression tests, model
  parsing tests, session mapping tests, and log-redaction coverage.

### Beta limitations

- `transport` is experimental and is not a general OpenAI API endpoint. The
  ChatGPT Codex backend shapes used by the open-source Codex client may
  change. `app_server` remains the recommended backend.
- ChatGPT Plus and the OpenAI API are separate products with separate
  authorization and billing. A Plus subscription is not represented as an
  OpenAI API key or API quota by this plugin.
- The server's `model/list` and rate-limit responses are authoritative. Local
  Usage statistics describe responses observed by this plugin; they do not
  replace the account's server-side quota page.
- Codex local shell, filesystem write, MCP, browser/computer control, and
  other Codex-native capabilities remain disabled by default. AstrBot owns
  the outer agent/tool policy.
