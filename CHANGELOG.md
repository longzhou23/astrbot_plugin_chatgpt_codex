# Changelog / 更新日志

本文件按仓库实际提交历史整理。当前只有 `v0.3.0-beta.1` 这一个正式 Git
标签；`0.1.0` 和 `0.2.0` 是对应历史阶段的开发里程碑，并不是曾经单独
发布过的公开标签。

This changelog follows the repository's actual commit history. The repository
currently has one formal Git tag, `v0.3.0-beta.1`; `0.1.0` and `0.2.0` are
historical development milestones, not separately published public tags.

## Unreleased / 未发布

### 中文

- 增加中英文双语版本记录。
- 增加 `README.zh-CN.md`，覆盖安装、配置、登录、Usage、安全、故障排查和
  Beta 限制。
- 在英文 README 顶部增加中文文档入口。
- 将首个 Beta 实现和配套文档同步到仓库默认 `main` 分支。

### English

- Added bilingual release notes.
- Added `README.zh-CN.md` covering installation, configuration, login, Usage,
  security, troubleshooting, and Beta limitations.
- Added a link to the Chinese documentation at the top of the English README.
- Synchronized the first Beta implementation and its documentation on the
  repository's default `main` branch.

## 0.3.0-beta.1 — 2026-08-25 / 第一个公开 Beta

### 中文

首个公开 Beta 版本。默认使用稳定的 Codex App Server，同时加入实验性的
Responses HTTP/SSE Transport 路径。

#### 新增和改进

- 默认使用 `codex app-server`，通过 JSONL RPC 完成账号、模型、配额、thread
  和 turn 管理。
- 增加实验性的直接 Codex Responses HTTP/SSE Transport；该模式不创建 Codex
  thread 或 turn，适合轻量请求和协议对比。
- 增加 ChatGPT OAuth 和 Device Code 登录，使用独立且持久化的 `CODEX_HOME`。
- 动态读取账号、套餐、配额、模型和 reasoning effort，不硬编码模型 ID。
- 完善 AstrBot Provider 集成、会话映射、单会话串行、多会话并行、重置和流式
  文本响应。
- Usage 基于服务端真实响应统计，支持输入、缓存输入、输出、总量、缓存命中
  Token、缓存命中率和最近请求详情。
- 增加概览和设置 WebUI 页面，支持 AstrBot 风格黑白蓝主题、亮色模式、暗色模式
  和 OAuth 回调地址交回。
- 默认关闭 Codex shell、文件写入、MCP、browser/computer control 等本机能力。
- 增加 JSONL RPC、SSE、Transport、Usage、模型解析、会话映射和日志脱敏测试。

#### Beta 限制

- `transport` 是实验性后端，不是通用 OpenAI API；其服务端接口形状可能随开源
  Codex 客户端更新而变化，稳定使用建议选择 `app_server`。
- ChatGPT Plus 和 OpenAI API 是两套独立的产品、授权和计费体系；Plus 不等同于
  OpenAI API Key 或 API 额度。
- `model/list` 和账号限流接口返回的数据由服务端决定；本地 Usage 只统计插件
  实际观察到的响应，不替代账号官方配额页面。
- Codex 本地 shell、文件系统写入、MCP、浏览器/电脑控制和其他内置能力默认关闭。

### English

First public Beta release. The stable Codex App Server remains the default,
with an experimental Responses HTTP/SSE Transport path for lightweight tests.

#### Added and improved

- Uses `codex app-server` by default for account, model, quota, thread, and turn
  management over JSONL RPC.
- Added an experimental direct Codex Responses HTTP/SSE Transport that does not
  create Codex threads or turns, intended for lightweight requests and protocol
  comparison.
- Added ChatGPT OAuth and Device Code login with an isolated persistent
  `CODEX_HOME`.
- Added dynamic account, plan, quota, model, and reasoning-effort discovery;
  model IDs are not hard-coded.
- Improved the AstrBot Provider integration, session mapping, serialized turns
  per session, concurrent sessions, reset behavior, and streaming text output.
- Added Usage accounting from real server response usage, including input,
  cached input, output, total, cache-hit tokens, cache-hit rate, and recent-turn
  details.
- Added overview and settings WebUI pages with AstrBot-style black, white, and
  blue visuals, light/dark modes, and OAuth callback handoff.
- Kept Codex shell, filesystem writes, MCP, browser/computer control, and other
  local capabilities disabled by default.
- Added tests for JSONL RPC, SSE, Transport, Usage, model parsing, session
  mapping, and log redaction.

#### Beta limitations

- `transport` is experimental, not a general OpenAI API, and its server-side
  shape may change with future open-source Codex client releases. Use
  `app_server` when reliability matters.
- ChatGPT Plus and the OpenAI API are separate products with separate
  authorization and billing; Plus is not an OpenAI API key or API quota.
- `model/list` and account rate-limit responses are authoritative. Local Usage
  only counts responses observed by this plugin and does not replace the
  account's official quota page.
- Codex local shell, filesystem writes, MCP, browser/computer control, and other
  built-in capabilities remain disabled by default.

## 0.2.0 — Development milestone / 开发里程碑

### 中文

这一阶段重点完善本地 Usage 统计、累计量处理、缓存字段和轻量聊天 Harness。

#### Usage 与可靠性

- 修复 Codex `tokenUsage.last` 与 `tokenUsage.total` 混用造成的累计量重复计算。
- 使用唯一 `turn_id` 保护重连和进程重启后的重复事件，避免同一个 turn 被重复
  计入统计。
- 保留服务端真实的 `totalTokens`，将缓存输入作为输入明细，不再次加到总量中。
- 增加输入、缓存输入、输出、推理输出、总量、请求数和缓存命中相关的统计模型。
- 增加 Usage 保留周期、时区、自然日聚合和最近请求明细。
- 增加对服务端大小写字段和部分缺失 usage 字段的兼容处理，不根据文本长度虚构
  Token 数量。

#### 轻量 Harness

- 增加 `lightweight` Harness，减少不必要的 Codex coding-agent 提示和可选上下文
  来源。
- 将 AstrBot 人设作为独立的 developer instructions 传入，并在提示版本变化时
  自动轮换 thread。
- 保留 `codex` Harness 作为需要原生 Codex coding-agent 行为时的可选模式。
- 增加真实 App Server prompt overhead benchmark 和对应测试。
- 继续保持 AstrBot 作为外层 Agent Runner，避免重复套用第二个 Agent Loop。

### English

This milestone focused on local Usage accounting, cumulative-token handling,
cache fields, and the lightweight chat Harness.

#### Usage and reliability

- Fixed duplicate cumulative accounting caused by mixing Codex
  `tokenUsage.last` and `tokenUsage.total` snapshots.
- Added unique `turn_id` protection so reconnects and process restarts do not
  count the same turn more than once.
- Preserved the server-authoritative `totalTokens` value and kept cached input
  as a breakdown instead of adding it to the total again.
- Added Usage models for input, cached input, output, reasoning output, total,
  request count, and cache-hit metrics.
- Added Usage retention, timezone-aware daily aggregation, and recent-turn
  details.
- Added compatibility for server field casing and partially missing usage data;
  the plugin never invents Token counts from text length.

#### Lightweight Harness

- Added a `lightweight` Harness that reduces unnecessary Codex coding-agent
  instructions and optional context sources.
- Passed AstrBot persona instructions as separate developer instructions and
  rolled the thread when the prompt version changed.
- Kept `codex` Harness as an opt-in mode for native Codex coding-agent behavior.
- Added a real App Server prompt-overhead benchmark and related tests.
- Kept AstrBot as the outer Agent Runner to avoid nesting a second Agent Loop.

## 0.1.0 — Initial MVP milestone / 初始 MVP 里程碑

### 中文

完成 AstrBot ChatGPT Codex Bridge 的初始 MVP，实现从 AstrBot Provider 到
Codex App Server 的最小可运行链路。

#### 核心能力

- 增加 AstrBot 插件结构、Provider 适配器、配置 schema、依赖和基础文档。
- 增加异步 JSONL RPC 客户端，支持 request ID、pending futures、通知分发、
  超时、取消和并发请求。
- 增加 Codex 进程管理器，负责查找、启动、停止 `codex app-server`，隔离
  `CODEX_HOME`、记录脱敏 stderr、健康状态和崩溃恢复。
- 增加 ChatGPT OAuth / Device Code 登录、登出、账号读取和登录状态处理。
- 增加 `account/read`、`account/rateLimits/read`、`model/list` 和 reasoning
  effort 读取。
- 增加 AstrBot 会话到 Codex thread 的持久化映射、恢复、轮换和重置。
- 支持普通文本和流式事件，过滤隐藏思维链、原始工具输出和内部状态。
- 默认关闭 Codex 本机 shell、文件写入、MCP、浏览器和电脑控制。
- 预留 ToolBridge 接口，但默认不把 AstrBot 工具传入 Codex，避免双 Agent Loop。

#### WebUI 与测试

- 增加账号概览、登录、退出、模型、配额和设置页面。
- 增加服务端模型动态发现和非敏感模型缓存。
- 增加 session store、Usage storage、错误分类和日志脱敏。
- 增加 RPC、模型目录、OAuth 回调、Provider、会话、安全和流式响应测试。

### English

Completed the initial MVP for the AstrBot ChatGPT Codex Bridge, providing the
minimum working path from an AstrBot Provider to the Codex App Server.

#### Core capabilities

- Added the AstrBot plugin structure, Provider adapter, configuration schema,
  dependencies, and baseline documentation.
- Added an async JSONL RPC client with request IDs, pending futures,
  notification dispatch, timeouts, cancellation, and concurrent requests.
- Added a Codex process manager for locating, starting, and stopping
  `codex app-server`, isolating `CODEX_HOME`, redacting stderr logs, reporting
  health, and recovering from crashes.
- Added ChatGPT OAuth / Device Code login, logout, account reading, and login
  state handling.
- Added `account/read`, `account/rateLimits/read`, `model/list`, and reasoning-
  effort discovery.
- Added persistent AstrBot-session to Codex-thread mapping, resume, rollover,
  and reset behavior.
- Supported normal text and streaming events while filtering hidden reasoning,
  raw tool output, and internal state.
- Disabled Codex local shell, filesystem writes, MCP, browser, and computer
  control by default.
- Reserved a ToolBridge interface, while keeping AstrBot tools out of Codex by
  default to avoid a double Agent Loop.

#### WebUI and tests

- Added account overview, login, logout, model, quota, and settings pages.
- Added dynamic server model discovery and a non-sensitive model cache.
- Added session storage, Usage storage, error classification, and log redaction.
- Added tests for RPC, model catalog, OAuth callback, Provider, sessions,
  security, and streaming responses.
