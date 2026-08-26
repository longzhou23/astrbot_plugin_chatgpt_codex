# AstrBot ChatGPT Codex Bridge

[English README](README.md)

这是一个 AstrBot 插件，让 AstrBot 通过官方 Codex 登录能力使用当前
ChatGPT 账号实际开放的 Codex 模型。插件推荐默认使用轻量的实验性
Responses HTTP/SSE Transport 后端；稳定的 `codex app-server` 作为兼容回退保留。

插件不使用 ChatGPT 网页 Cookie、浏览器抓包、私有 BFF 接口，也不伪造
OpenAI API。当前版本为第一个公开 Beta：`v0.3.0-beta.1`。

## 首次启动指引（新实例必读）

首次登录有一个容易忽略的前置条件：

### 1. 先安装 Codex CLI

请以 [OpenAI 官方 Codex CLI 安装/开始使用文档](https://learn.chatgpt.com/docs/codex/cli)
为准。常用安装方式如下：

**macOS / Linux（独立安装脚本）：**

```bash
curl -fsSL https://chatgpt.com/codex/install.sh | sh
```

**Windows、macOS 或 Linux（npm）：**

```bash
npm install -g @openai/codex
```

npm 方式需要先安装可用的 Node.js/npm。安装后请重新打开终端，并确认命令
可执行：

```text
codex --version
```

这个检查必须使用与 AstrBot 相同的操作系统用户执行。如果 AstrBot 服务账号
找不到 `codex`，请在插件设置中把 `codex_path` 填成可执行文件的绝对路径，例如
Windows 的 `C:\\Users\\<user>\\AppData\\Roaming\\npm\\codex.cmd`，或
Linux/macOS 的 `/usr/local/bin/codex`。首次 ChatGPT 登录使用官方 Codex App
Server 提供的 OAuth / Device Code RPC。

插件第一次打开概览页时会显示欢迎配置页。它会先自动检查 `codex` 是否在当前
AstrBot 运行环境的 PATH 中；如果检测成功，保持 `codex` 即可。只有检测失败时
才需要填写绝对路径。欢迎页也可以直接选择浏览器 OAuth 或 Device Code，并保存
配置后开始登录。

#### Docker 用户特别说明

AstrBot 在 Docker 容器内启动 Codex，插件只能看到容器内的文件和 PATH，不能直接
使用宿主机的 `/usr/bin/codex`、`/usr/lib/node_modules` 或 Windows 路径。请进入
实际运行 AstrBot 的容器检查：

```bash
docker compose exec astrbot sh
command -v codex
codex --version
```

如果 Codex 安装在容器内但不在 PATH 中，把 `command -v codex` 得到的容器内绝对
路径填入欢迎页或设置页。若需要通过 npm 安装，请把安装步骤写入自己的 Dockerfile
并重新构建镜像，不要只在正在运行的容器里临时安装：

```dockerfile
RUN npm install -g --include=optional @openai/codex@0.149.1
```

`CODEX_HOME` 会由插件保存到 AstrBot 的持久化数据目录；请确保 Docker Compose
挂载了 `/AstrBot/data`。首次登录仍需在插件欢迎页完成一次 ChatGPT OAuth，之后
不需要每次启动都重新登录。不要把宿主机 Codex 路径直接复制到容器配置中。

### 2. 完成首次登录

1. 重启 AstrBot，打开插件 WebUI 的概览页面，点击 ChatGPT 登录并完成浏览器
   OAuth 或 Device Code 授权。登录方式会使用欢迎页/设置页中当前保存的选择；
   Device Code 会直接显示验证地址和一次性验证码。
2. 如果浏览器最后跳转到 `localhost` 回调地址，将地址栏中的完整 URL 粘贴回插件
   页面，必须包含完整的 `?code=...&state=...` 参数；不要只粘贴路径或 code，也
   不要把回调地址发到日志或 Issue 中。远程服务器上浏览器所在电脑的 localhost
   不是服务器本机，因此需要手动提交完整回调。
3. 页面显示账号已登录后，登录态会持久化到插件独立的 `CODEX_HOME`。此后推荐的
   `transport` 后端可以读取登录态并直接进行 Responses 推理，不会每次请求都启动
   `codex app-server`。

如果新实例显示 `No such file or directory: 'codex'` 或
`Unable to start Codex app-server`，说明 Codex CLI 尚未安装，或者 `codex_path`
填写错误，请先修复这个前置条件。选择 `app_server` 时推理本身也需要 Codex
可执行文件；选择 `transport` 只有在首次官方登录完成后，才可以不启动 App Server
进行推理。

插件不会要求用户复制 ChatGPT Cookie、access token、refresh token 或密码。不要
手动编辑或分享 `CODEX_HOME/auth.json`。

## 重要说明：Plus 不等于 OpenAI API

ChatGPT Plus 和 OpenAI API 是两套独立的产品、授权和计费体系：

- ChatGPT Plus 不是 OpenAI API Key。
- 本插件不会把 Plus 订阅声明为 OpenAI API 额度。
- 模型、套餐和账号配额以 Codex 服务端返回的数据为准。
- 本地 Usage 页面统计的是插件实际观察到的请求，不替代账号服务端的官方配额页面。

## 当前后端

### `transport`：默认、推荐

Transport 直接发送 Codex Responses HTTP/SSE 请求，不创建 Codex thread 或
turn，也不启动 App Server 作为推理后端。它是当前推荐的轻量聊天路径，适合
AstrBot 普通对话；服务端接口形状来自开源 Codex 客户端实现，未来可能随 Codex
更新变化。

Transport 不提供 Codex 本地 shell、文件系统、MCP、浏览器、电脑控制或其他
内置工具。AstrBot 选择的函数工具只会以结构化工具调用返回给 AstrBot，当前
MVP 不执行 Transport 侧的多轮工具循环。

### `app_server`：稳定兼容回退

插件启动 `codex app-server`，使用当前版本默认的 stdio 传输，通过 Codex App Server 的 JSONL RPC
协议完成登录、模型发现、thread 管理和 turn 流式响应。需要稳定的 Codex
Agent Loop 或 Transport 不可用时，可以在设置中显式选择此后端。

### `auto`

`auto` 会先尝试 Transport，遇到认证、模型、协议、网络或限流失败时回退到
App Server。每次请求只进行一次回退；配额耗尽不会无限重试。

如果希望由插件自动尝试 Transport 并在失败时回退，请选择 `auto`。如果目标是
使用稳定的 Codex Agent Server 协议，请显式选择 `app_server`。

## 安装

### 从 GitHub 安装 Beta

```bash
git clone --branch v0.3.0-beta.1 --depth 1 \
  https://github.com/longzhou23/astrbot_plugin_chatgpt_codex.git
```

将插件目录复制到 AstrBot 的：

```text
data/plugins/astrbot_plugin_chatgpt_codex
```

也可以直接下载 [v0.3.0-beta.1 源码压缩包](https://github.com/longzhou23/astrbot_plugin_chatgpt_codex/archive/refs/tags/v0.3.0-beta.1.zip)。

安装后重启 AstrBot，并确认插件已经启用。

## 前置条件

1. 在运行 AstrBot 的主机上安装可执行的 Codex CLI。
2. 确认命令行可以执行 `codex`，或者在设置中填写绝对路径。Transport 推理本身
   不启动 App Server，但首次 ChatGPT OAuth / Device Code 登录仍通过官方
   `codex app-server` 登录 RPC 完成。
3. `app_server` 推理启动 `codex app-server`，使用当前版本默认的 stdio 传输。插件不再
   传递旧版本的 `--stdio` 参数，避免当前 Codex CLI 因不认识该参数而立即退出。
4. AstrBot 进程需要能够访问 Codex 登录和推理服务。
5. 如果主机必须通过代理访问网络，默认会继承 AstrBot 进程的
   `HTTP_PROXY`、`HTTPS_PROXY`、`ALL_PROXY` 环境变量；也可以在插件设置中填写明确的
   `transport_proxy`，例如 `http://127.0.0.1:7890`，它会覆盖系统代理。

OAuth token exchange、Device Code 申请、Responses Transport 和 App Server
   都使用同一套代理规则。代理地址中不要写入用户名或密码。Docker 用户必须把代理
   环境变量传入容器；容器内的 `127.0.0.1` 指向容器本身，不是宿主机。

## WebUI 使用方法

插件页面只有两个主要页面：

- **概览**：账号信息、套餐、配额窗口、重置时间、Usage 汇总、缓存命中情况、
  最近请求、服务端模型和运行状态。
- **设置**：中文配置项、后端选择、Codex 路径、登录方式、推理强度、并发数、
  超时时间、代理和本机工具开关。

进入概览或刷新页面时，插件会重新获取账号状态、服务端模型和可用配额信息。
如果没有登录，页面会显示登录入口；浏览器 OAuth 完成后，将回调页面显示的
`localhost` 链接交回插件页面，以完成服务端登录确认。

### 登录

登录态保存在插件独立的 Codex 数据目录中，不会写入 AstrBot 项目根目录，
也不会把 access token、refresh token 或 device code 写入普通日志。

浏览器登录流程：

1. 在概览页面点击登录。
2. 打开页面提供的 OAuth 地址并完成 ChatGPT 登录。
3. 如果浏览器最后跳转到 `localhost` 回调地址，将完整回调地址粘贴回插件页面。
4. 等待页面显示已登录，然后刷新概览和模型列表。

如果服务器没有图形浏览器，可以在设置中选择 Device Code 登录方式，并按页面
提示完成授权。

## 配置项

常用配置如下：

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `codex_path` | `codex` | Codex 可执行文件名或绝对路径 |
| `backend_mode` | `transport` | `transport`（推荐）、`app_server`（稳定回退）或 `auto` |
| `transport_proxy` | 空 | 显式 HTTP/HTTPS 代理；优先级高于系统代理 |
| `use_system_proxy` | `true` | 继承 AstrBot 进程的 HTTP_PROXY / HTTPS_PROXY / ALL_PROXY |
| `login_mode` | `browser` | 浏览器 OAuth 或 `device_code` |
| `default_model` | `auto` | 服务端 `model/list` 返回的模型 ID |
| `reasoning_effort` | `auto` | 服务端声明的推理强度 |
| `harness_mode` | `lightweight` | 聊天精简外壳或原生 Codex 外壳 |
| `streaming` | 开启 | AstrBot 支持时使用流式响应 |
| `max_concurrent_turns` | `2` | 全局并发 turn 数上限 |
| `turn_timeout` | `600` | 单次请求超时时间，单位为秒 |
| `enable_local_codex_tools` | 关闭 | 是否允许 Codex 使用本机工具，安全默认关闭 |
| `usage_timezone` | `Asia/Shanghai` | 本地 Usage 自然日的时区 |
| `usage_retention_days` | `365` | 本地 Usage 记录保留天数 |

模型 ID 和 reasoning effort 不在插件中硬编码，服务端返回什么就显示什么。

## Usage 统计

概览页面展示插件本地观察到的请求统计，包括：

- 今日、近 7 日、近 30 日和累计 Token。
- 输入 Token。
- 缓存输入 Token。
- 输出 Token。
- 总 Token。
- 缓存命中 Token。
- 缓存命中率。
- 最近请求的逐条输入、缓存、命中率、输出和总量。

Usage 使用服务端响应中实际返回的 usage 字段，不根据上下文长度或提示词字符
数估算 Token。Transport 模式只有在 Responses 流返回真实 usage 时才记录用量。
如果服务端没有返回 usage，页面不会虚构数值。

配额卡片和本地 Usage 是不同数据源：配额卡片来自账号服务端；Usage 是本地
插件记录。两者的时间窗口、统计口径和刷新时间可能不同。

## 命令

管理员可使用以下命令：

```text
/gpt status       查看登录、后端和运行状态
/gpt login        开始 ChatGPT 登录
/gpt logout       退出登录
/gpt models       刷新并查看服务端模型
/gpt model <id>   选择模型
/gpt effort <级别> 设置 reasoning effort
/gpt quota        查看账号配额
/gpt reset        重置当前 AstrBot 会话对应的 Codex thread
```

敏感操作（登录、退出登录、切换模型和推理强度）限制为管理员。WebUI 是推荐
的配置和登录入口，命令主要用于管理和排障。

## 安全边界

默认情况下：

- Codex 本机 shell 关闭。
- 文件系统写入关闭。
- Codex MCP 关闭。
- browser/computer control 关闭。
- 本机工具开关 `enable_local_codex_tools` 关闭。
- Transport 不暴露 Codex 本机能力面；选择 `app_server` 时使用只读、无网络的
  沙箱策略。
- 不显示隐藏思维链、原始 reasoning、命令文本、内部状态或原始工具输出。
- 日志会进行脱敏，不记录 access token、refresh token、device code、Cookie 或
  完整认证 URL。

开启本机 Codex 工具会扩大 AstrBot 进程的本机权限，仅建议在隔离测试环境中使用。

## 会话和响应行为

插件使用 AstrBot 提供的统一 `session_id` 映射 Codex thread：

- 同一会话内的请求串行执行。
- 不同会话可以并行执行。
- `/gpt reset` 会重置当前会话映射。
- 人设、系统提示和上下文由 AstrBot 外层 Agent Runner 管理。
- Transport 是默认的轻量路径；插件不会在 Transport 内部套第二个 Agent Loop。
- App Server 是稳定兼容回退；插件不会在 App Server 外部再套一层重复的 Agent Loop。
- Transport 会将 AstrBot 工具调用返回给 AstrBot，并保留工具结果、结构化 function call 历史、图片输入和 Responses opaque reasoning 状态供下一轮使用；插件本身不会再套第二个 Transport Agent Loop。

AstrBot 的 `Context.llm_generate()` 对插件后台调用不一定提供 `session_id`。这类调用会使用每次请求独立生成的一次性会话键，并在请求结束后清理本地映射，绝不会与正常聊天或其他插件共享 Codex thread。

## 常见问题

### 页面提示未授权或会话过期

确认访问的是当前 AstrBot WebUI 地址，并且浏览器登录态仍有效。重新打开概览
页面，点击登录；如果 OAuth 最后跳转到 `localhost`，需要把完整回调地址复制回
插件页面，不能只完成浏览器跳转后直接关闭页面。

### `codex` 找不到

在设置中把 `codex_path` 改成 Codex 可执行文件的绝对路径，然后重启 AstrBot。

### Transport 连接失败或响应很慢

如果 Transport 或登录连接失败，先确认 `use_system_proxy` 已开启且 AstrBot 进程确实拥有
`HTTP_PROXY` / `HTTPS_PROXY` / `ALL_PROXY`，或者填写 `transport_proxy`（如网络必须通过代理），或将
`backend_mode` 改为稳定的 `app_server`。Transport 是实验功能，网络、鉴权和
服务端接口变化都可能导致失败。

### Usage 显示为 0

确认本次请求确实返回了服务端 usage，并刷新概览。历史请求不会根据上下文长度
补算；没有真实 usage 的请求会保持为空或不计入统计。

### 配额和本地 Usage 不一致

这是允许的：配额是服务端账号窗口，本地 Usage 是插件观察到的请求记录。请以
服务端配额为准，不要把本地统计当作官方剩余额度。

## 开发和测试

在插件目录运行：

```bash
python -m pytest -q
ruff check .
python -m compileall -q .
git diff --check
```

当前 Beta 发布前已通过全部测试、静态检查和 Python 编译检查。

## Beta 限制

- 推荐使用 `transport`；`app_server` 是稳定兼容回退。
- Codex 客户端或服务端接口变化可能影响 Transport。
- Transport 仍由 AstrBot Agent Runner 负责工具执行和循环，插件不会再创建第二个工具循环；但工具结果和结构化调用历史会完整交给下一次 Transport 请求。
- 账号官方配额仍由 Codex 服务端决定，插件不能增加订阅额度。
- ChatGPT Plus 不包含 OpenAI API 余额或 API Key。

## 许可证和反馈

本项目为 AstrBot 插件，欢迎通过 GitHub Issues 反馈登录、模型、配额、Transport
和 UI 问题。反馈时请提供脱敏后的插件日志、AstrBot 版本、Codex 版本和后端模式，
不要粘贴 token、Cookie、完整 OAuth URL 或个人隐私信息。
