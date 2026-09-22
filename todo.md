# AstrBot 模型调用兼容性 TODO

本文档根据 AstrBot 当前文档、AstrBot `master` 调用契约和本插件 `v1.0.0` 实现审计整理。

审计基线：

- 插件：`astrbot_plugin_chatgpt_codex` `v1.0.0`
- 插件提交：`40fb4bb`
- AstrBot 提交：`fe3d77568b88ea3be83b2190d515da0a039da399`
- 当前自动测试：105 项通过
- 当前插件定位：AstrBot `chat_completion` Provider
- 推荐后端：Responses HTTP/SSE Transport

> P0 实现状态（本次）：核心代码、回归测试和静态检查已完成。当前工作区没有运行中的 AstrBot 实例，因此 `/stats` 与 Iris Memory 的现场验收仍需在部署实例上完成，不能用本地单元测试替代。

## 目标

- 完整满足 AstrBot 当前 Chat Provider 调用契约。
- 保证普通聊天、流式输出、Persona、上下文、多模态和 AstrBot Agent Runner 工具循环行为一致。
- Transport 与 App Server 的能力差异必须显式可见，不能静默丢失工具或输入。
- 插件内部 Usage 与 AstrBot 标准 `LLMResponse.usage` 使用同一份服务端数据。
- 不引入第二套 Agent Loop。AstrBot 工具仍由 AstrBot Agent Runner 执行。
- 不暴露 hidden reasoning、token、OAuth 凭据或其他内部状态。

## 非目标

本插件当前只注册 `chat_completion` Provider。以下能力不应伪装成聊天模型能力：

- Speech-to-Text / STT
- Text-to-Speech / TTS
- Embedding
- Rerank

上述类型继续由 AstrBot 中独立的专用 Provider 提供。如未来需要支持，应分别实现对应 Provider 接口和配置项。

## P0：影响现有功能正确性

### 1. 向 AstrBot 回传标准 Token Usage

- [x] 在 Transport 终态事件中保留服务端 Usage。
- [x] 在 App Server 终态事件中保留本轮 Usage 或可靠的本轮增量。
- [x] 将服务端 Usage 转换成 AstrBot `TokenUsage`：
  - `input_other = input_tokens - cached_input_tokens`
  - `input_cached = cached_input_tokens`
  - `output = output_tokens`
- [x] 在最终非 chunk `LLMResponse` 中设置 `usage=TokenUsage(...)`。
- [x] 工具调用终态也必须携带本轮 Usage，不能只给最终文本轮次统计。
- [x] 不把 reasoning token 再次累加到 output，避免重复统计。
- [x] 缺少 Usage 时返回 `None`，不能伪造为 0。
- [x] 保留现有本地 Usage Dashboard 数据来源和去重逻辑。
- [ ] 验证 AstrBot `/stats` 能显示输入、缓存输入和输出。（需在运行中的 AstrBot 实例验收）
- [ ] 验证 Iris Memory 等直接 LLM 调用不再记录 `tokens=0+0`。（需在运行中的 AstrBot 实例验收）

验收标准：

- 插件概览、本地 Usage 数据库和 AstrBot `/stats` 对同一轮请求的总量口径一致。
- `LLMResponse.usage` 在普通文本、非流式、流式终态和工具调用终态中均可读取。
- 重试、重复 SSE 终态和 App Server 累计快照不会造成重复记账。

### 2. 阻止 App Server 静默丢失 AstrBot 工具

- [x] 删除或替代 App Server 路径中无提示的 `del tool_calls_result, tools` 行为。
- [x] 在尚未完成 App Server ToolBridge 前，只要请求包含 `tools` 或 `tool_calls_result`，就禁止从 Transport 自动回退到 App Server。
- [x] 返回可识别的能力错误，例如“App Server 后端暂不支持 AstrBot 工具调用”。
- [x] WebUI 在选择 `app_server` 时显示工具兼容性警告。
- [x] WebUI 在选择 `auto` 时说明工具请求不会回退到 App Server。
- [x] 普通无工具文本请求仍可按配置回退。
- [x] 不允许 App Server 自己执行本地 shell、文件写入、MCP 或浏览器工具来替代 AstrBot 工具。

验收标准：

- Transport 故障时，星图、网页搜索、知识库、MCP 和图片标注请求不会变成无工具的普通回答。
- 工具不可用时必须明确报错，不能返回空 assistant，也不能假装工具执行成功。
- 整个调用链中只有 AstrBot Agent Runner 执行工具循环。

### 3. 适配 AstrBot Provider 调用参数

- [x] 在 `text_chat()` 和 `text_chat_stream()` 中显式接收 `tool_choice`。
- [x] 支持 `tool_choice="auto"`。
- [x] 支持 `tool_choice="required"`，并正确映射到 Responses 请求。
- [x] 在没有工具时忽略或拒绝 `required`，给出明确错误。
- [x] 显式接收并处理 `request_max_retries`。
- [x] 让调用方给出的重试上限覆盖默认策略，但额度耗尽和鉴权错误不得重试。
- [x] 处理 AstrBot 的 `abort_signal`，确保 `/stop` 能中断当前 HTTP/SSE 请求和 App Server Turn。
- [x] 不再直接 `del kwargs`。
- [x] 建立支持参数白名单。
- [x] 对不支持的参数返回明确提示，不能静默忽略。

首批需要评估的可选参数：

- `temperature`
- `top_p`
- `max_tokens` / `max_output_tokens`
- `stop`
- `response_format`
- `parallel_tool_calls`
- `tool_choice`
- `request_max_retries`
- `abort_signal`

验收标准：

- `context.llm_generate(..., tool_choice="required")` 的行为可预测且有测试。
- `request_max_retries=1` 不会继续执行插件内部的多次网络重试。
- 不支持的参数不会被悄悄吞掉。
- `/stop` 后不再继续输出文本、执行工具或保留运行中的请求。

### 4. 消除空 assistant 终态

- [x] 普通文本响应必须产生一个带完整文本的最终非 chunk `LLMResponse`。
- [x] 工具调用响应必须产生结构化 tool-call 终态，不能再附加空 assistant 终态。
- [x] refusal 必须作为公开文本或明确错误返回。
- [x] incomplete/failed/error SSE 事件必须映射为可诊断错误。
- [x] Transport 和 App Server 都要拒绝“无文本且无工具调用”的空响应。
- [x] 错误信息保持脱敏。

验收标准：

- 正常聊天、搜索、星图、重复图片、知识型问题不再触发：
  `LLM returned empty assistant message with no tool calls.`
- 不重复输出最终文本。

## P1：补齐 Chat Provider 契约

### 5. 支持 Transport 本地音频路径

- [ ] 支持 AstrBot `audio_urls` 中的本地路径。
- [ ] 支持安全的 `file://localhost/...` 音频路径。
- [ ] 校验文件存在、大小上限和真实音频格式。
- [ ] 转换为服务端实际支持的音频输入格式。
- [ ] 拒绝 UNC、远程主机 `file://` 和非音频文件。
- [ ] 不在日志中输出音频内容或 data URI。
- [ ] 如果当前 Codex 模型不支持音频，返回明确能力错误。
- [ ] 在完成真实端到端验证前，评估是否暂时从 Provider `modalities` 中移除 `audio`。

验收标准：

- `context.llm_generate(audio_urls=[本地路径])` 能让模型实际读取音频，而不是只收到 `[音频附件]`。
- HTTP(S)、data URI 和本地路径都有测试。
- 超限、损坏和不支持格式有明确错误。

### 6. 动态设置模型上下文窗口

- [ ] 从 `model/list` 或 Transport 模型元数据中解析上下文窗口字段。
- [ ] 将上下文窗口保存到 `CodexModel`。
- [ ] 模型缓存保留上下文窗口字段。
- [ ] Provider 切换模型后同步更新 `max_context_tokens`。
- [ ] 服务端没有返回上下文窗口时使用保守兜底值。
- [ ] 支持用户手动覆盖，并在 WebUI 标明是人工值。
- [ ] 不再默认把所有模型声明为 1,000,000 Token。

验收标准：

- AstrBot Agent Runner 能在接近真实模型限制前触发上下文压缩。
- 切换不同模型后压缩阈值同步变化。
- 未知模型不会因为虚假的 1,000,000 上限而持续堆积上下文。

### 7. 使用 `LLMResponse.result_chain`

- [ ] 普通文本终态使用 `MessageChain` 返回。
- [ ] 流式文本 delta 使用 `MessageChain` 返回。
- [ ] 保留 `completion_text` 兼容旧版 AstrBot，但以 `result_chain` 为主。
- [ ] refusal 使用公开文本组件返回。
- [ ] 为将来的图片、文件和音频结果预留标准组件映射。
- [ ] 工具调用字段与 `result_chain` 可以同时存在。

验收标准：

- 当前 AstrBot 和最低支持版本均能正常读取文本。
- 不依赖 AstrBot 对过时 `completion_text` 的长期兼容。

### 8. 完善消息组件适配

- [ ] 文本、图片、音频、引用、@、表情、位置、分享、JSON/XML 和转发消息分别建立测试样例。
- [ ] 明确文件和视频目前是“元数据标记”而不是原始内容输入。
- [ ] 对 PDF、Word、文本文件等优先通过 AstrBot 文件读取工具处理。
- [ ] 工具返回的图片必须作为图片组件重新进入下一轮上下文。
- [ ] 工具直接返回给用户的图片必须保持 AstrBot 消息链格式。
- [ ] 防止当前图片同时出现在 `contexts` 和 `image_urls` 时重复发送。
- [ ] 防止用户再次发送相同图片时错误删除当前轮图片。
- [ ] App Server 不支持的远程媒体必须明确提示，不能静默忽略。

验收标准：

- 星图工具能读取当前图片、执行标注并把标注后的图片发给用户。
- 重复发送以前出现过的图片仍能正常识别。
- 图片预处理为空时能区分“模型不支持”“路径不可读”和“上游插件已经消费”。

### 9. 模型能力按实际模型声明

- [ ] 模型目录保存每个模型支持的输入模态。
- [ ] 模型目录保存每个模型支持的 reasoning efforts。
- [ ] 模型目录保存工具调用能力。
- [ ] Provider `modalities` 与当前选中模型保持一致。
- [ ] 自动模型模式切换模型后刷新能力声明。
- [ ] 不支持视觉、音频或工具的模型在请求前失败并给出说明。

验收标准：

- 不再统一宣告所有模型都支持 `text/image/audio/tool_use`。
- 模型不支持某种输入时不会等到远端 HTTP 400 才发现。

### 10. 明确 ChatUI 自动标题策略

- [ ] 增加“允许生成 ChatUI 会话标题”配置项。
- [ ] 默认值根据额外用量和用户体验选择，并写入 README。
- [ ] 开启时允许 AstrBot 标题生成请求正常调用模型。
- [ ] 关闭时返回 `<None>`，但在日志中记录为有意跳过，而非模型错误。
- [ ] 标题请求继续使用临时 session，不能污染用户聊天 thread。

验收标准：

- 开启后 ChatUI 能生成简短标题。
- 关闭后不会产生额外 Codex turn。

## P2：兼容性、文档和长期维护

### 11. 验证最低 AstrBot 版本

- [ ] 核查 `astrbot_version: ">=4.13.0"` 是否真实成立。
- [ ] 至少测试最低支持版本、当前稳定版和当前 `master`。
- [ ] 检查以下 API 在最低版本中的可用性：
  - `ContentPart`
  - `Message`
  - `extra_user_content_parts`
  - `LLMResponse.result_chain`
  - `LLMResponse.usage`
  - Provider `modalities`
  - `tool_use`
- [ ] 如果无法兼容 4.13，提升 `metadata.yaml` 最低版本。
- [ ] README 写清楚经过实际验证的 AstrBot 版本范围。

### 12. 增加真实 AstrBot Contract Test

- [ ] 使用当前 AstrBot Provider 基类加载插件，不再只依赖 host-free stub。
- [ ] 验证 Provider 注册和卸载不会残留重复 adapter。
- [ ] 验证 `get_models()`。
- [ ] 验证 `text_chat()`。
- [ ] 验证 `text_chat_stream()`。
- [ ] 验证 `context.llm_generate()`。
- [ ] 验证 `context.tool_loop_agent()`。
- [ ] 验证 Persona、系统提示词和 `extra_user_content_parts`。
- [ ] 验证 Usage 回传。
- [ ] 验证 `/stop` 取消。
- [ ] 验证 Transport 故障和 App Server 回退规则。
- [ ] 真实 ChatGPT 账号测试必须可选，不得把凭据放入 CI。

### 13. 增加后端能力矩阵

- [ ] 在 README 中增加 Transport、App Server 和 Auto 的能力对照表。
- [ ] WebUI 根据当前后端显示以下状态：
  - 普通文本
  - 流式输出
  - 图片输入
  - 音频输入
  - AstrBot 工具
  - 工具结果续传
  - Usage
  - 是否需要 Codex CLI
- [ ] 设置页对会导致能力下降的选项显示非阻塞警告。
- [ ] 首次欢迎页默认推荐 Transport。

### 14. 非聊天模型需求文档化

- [ ] README 明确说明本插件不是 STT Provider。
- [ ] README 明确说明本插件不是 TTS Provider。
- [ ] README 明确说明本插件不能作为 Embedding Provider。
- [ ] README 明确说明本插件不能作为 Rerank Provider。
- [ ] 给出 AstrBot 中分别配置这些 Provider 的示意。
- [ ] 说明聊天模型接收音频附件不等于提供 STT 服务。

### 15. 错误分类与可观测性

- [ ] 区分网络错误、代理错误、鉴权错误、配额耗尽、模型不支持、媒体无效、工具不兼容和空响应。
- [ ] 配额耗尽不无限重试。
- [ ] 401/403 不无限重试。
- [ ] HTTP 400 显示经过脱敏的参数类别和服务端错误类型。
- [ ] 不记录 access token、refresh token、device code、OAuth code、完整 callback URL 或图片 data URI。
- [ ] 状态页显示当前实际使用的后端，而不是只显示配置值。
- [ ] Auto 回退发生时显示本轮是否失去某项能力。

## 回归测试清单

每次发布前至少验证：

- [ ] 普通短文本聊天。
- [ ] 长上下文聊天。
- [ ] Persona 风格保持。
- [ ] 非流式调用。
- [ ] 流式调用且不重复输出。
- [ ] `context.llm_generate()` 无 session ID。
- [ ] 多个并发无 session ID 调用互不共享上下文。
- [ ] `context.tool_loop_agent()` 单工具调用。
- [ ] 并行工具调用。
- [ ] 工具调用结果继续生成最终回答。
- [ ] 网页搜索。
- [ ] 知识库。
- [ ] AstrBot MCP。
- [ ] 星图识别。
- [ ] 星图标注图片回传。
- [ ] 当前图片首次发送。
- [ ] 同一图片重复发送。
- [ ] 本地 PNG/JPEG/GIF/WebP。
- [ ] HTTP(S) 图片。
- [ ] 图片 data URI。
- [ ] 本地音频。
- [ ] HTTP(S) 音频。
- [ ] 音频 data URI。
- [ ] 引用消息。
- [ ] 文件和视频降级提示。
- [ ] 模型切换。
- [ ] reasoning effort 切换。
- [ ] AstrBot `/stats`。
- [ ] 插件 Usage Dashboard。
- [ ] `/stop` 中断。
- [ ] Transport 网络失败。
- [ ] App Server 文本回退。
- [ ] 有工具请求时拒绝 App Server 回退。
- [ ] 配额耗尽不重试。
- [ ] OAuth 失效后错误脱敏。

## 发布门禁

以下条件全部满足后，才可以声明“完整兼容 AstrBot Chat Provider 调用需求”：

- [ ] P0 全部完成。
- [ ] Transport 普通聊天和工具循环通过真实 AstrBot 集成测试。
- [ ] `LLMResponse.usage` 与 AstrBot `/stats` 正常。
- [ ] 不再静默忽略 Provider 参数。
- [ ] App Server 工具能力限制在 WebUI 和 README 中明确可见。
- [ ] 本地音频已支持，或从能力声明中移除。
- [ ] 每模型上下文窗口不再固定为 1,000,000。
- [ ] 最低 AstrBot 版本经过实际验证。
- [ ] 所有自动测试、静态检查和 `git diff --check` 通过。
- [ ] README、README.zh-CN.md、README.en.md 和 CHANGELOG 同步更新。

## 参考资料

- AstrBot Agent Runner：https://docs.astrbot.app/use/agent-runner.html
- AstrBot 内置 Agent Runner：https://docs.astrbot.app/providers/agent-runners/astrbot-agent-runner.html
- AstrBot 插件 AI 调用：https://docs.astrbot.app/dev/star/guides/ai.html
- AstrBot Provider 基类：https://github.com/AstrBotDevs/AstrBot/blob/master/astrbot/core/provider/provider.py
- AstrBot Provider 实体：https://github.com/AstrBotDevs/AstrBot/blob/master/astrbot/core/provider/entities.py
