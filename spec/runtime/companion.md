<!-- SPDX-License-Identifier: Apache-2.0 -->
# Companion 机制

Companion 是运行在本机（或远端工作机）上的常驻桥接进程，用于把浏览器环境无法直接完成的“本机能力”提供给 OpenFocus。

典型例子：系统目录选择器返回**绝对路径**、托管 `coco/codex` 的交互式进程（PTY）、流式读写 stdin/stdout、列出由 OpenFocus 托管的 agent 会话等。

## 目标

- 让 OpenFocus 保持 Web 形态（local-first control plane），同时具备“像桌面应用一样”的本机能力。
- 支持多机：每台机器运行一个 Companion，统一接入到用户主机上的 OpenFocus，实现跨机器的 AgentSpace/会话托管。
- 所有动作可追溯：命令、输出、状态变更都可落库为 `events`，并进入 audit memory / daily memory / long-term memory 体系。

## 组件边界（Control Plane vs Data Plane）

- OpenFocus（Control Plane）：目标/任务/事件、推荐、Review、审计与状态机；对外暴露 Web UI 与 Core API。
- Companion（Data Plane）：执行本机操作与资源托管，负责：
  - 弹系统选择器（目录/文件）并返回绝对路径
  - 托管交互式 agent 进程（`coco`/`codex`），提供 PTY 与流式 I/O
  - 文件系统只读浏览/预览（可选；也可由 OpenFocus 直读 workspace，取决于部署）
  - 管理由 OpenFocus 启动的会话列表（`list_managed_agents`），支持 new session/释放

> 原则：Companion 默认只实现“白名单能力”，不做任意 shell 执行，避免演变成通用远控。

## 通讯模型

### 总体原则（统一采用长连接，避免反向可达性依赖）

- Companion **不提供 HTTP Web 服务**（不对外监听端口），仅作为客户端。
- Companion 以出站方式连接 OpenFocus（更易穿透 NAT/防火墙），由 OpenFocus 通过该长连接“下发命令”。
- 通讯协议统一采用 **gRPC 双向流（bidirectional streaming）**，用 **ping/pong** 机制确认心跳与在线状态。

### gRPC 通道职责（Control Plane ⇄ Data Plane）

- 连接建立：Companion 连接 OpenFocus 的 gRPC server，并发送 `hello/register`（device_id/name/capabilities/可选已配对 token）。
  - **身份标识**：OpenFocus 会为每个 Companion 分配一个稳定的 `companion_id`（服务端生成）。
    - Companion 首次连接时不携带 `companion_id`（或为空/0），OpenFocus 在 `welcome` 中回传分配结果。
    - Companion 必须把 `companion_id` 持久化到本地 state；后续重启/重连时必须携带该 `companion_id`，以便 OpenFocus 复用同一条设备记录。
    - 目的：避免“同一台节点重启后被识别为新的 Companion”，导致 UI/AgentSpace 绑定漂移。
- 心跳确认：
  - OpenFocus 定期发送 `Ping(ts)`；Companion 必须尽快回 `Pong(ping_ts)`。
  - OpenFocus 仅依据“长连接是否存活 + 最近一次 pong/消息时间”判定 `active/offline`。
- 命令下发（OpenFocus -> Companion）：
  - 例如：`choose_directory`、`spawn_agent(coco)`、`list_sessions`、`send_stdin`、`terminate_session` 等。
  - 每个命令必须带 `request_id`，以支持并发、多路复用与幂等。
- 结果/事件回传（Companion -> OpenFocus）：
  - 命令结果：`request_id` 对应的 `response`（ok/error + payload）。
  - 过程事件：stdout/stderr、阶段进度、会话状态变更等（可映射为 `/api/agent/events` 落库）。
  - runtime signal：Codex/Coco hooks、turn lifecycle、approval/input waiting、turn completed 等本机信号，通过 `AgentRuntimeSignal` 回传 Core，由 Core 更新 `agent_turns` / `task_agent_activity`。
- 断线重连：指数退避；重连后重新发送 `hello`；OpenFocus 以 `device_id` 关联同一设备。

### Enrollment 配对（认证码）在 gRPC 模型下的落点

- Companion 本地生成 10 位字母/数字认证码：
  - 每次用户点击认证后生成一个，有效期10分钟
  - 用户尝试输入后立即轮换一次
  - 每分钟最多尝试10次
- 用户在 OpenFocus UI 输入认证码后：
  - OpenFocus 通过 gRPC 向对应 Companion 下发 `pair_confirm(code)`
  - Companion 校验成功后返回 `auth_token`
  - OpenFocus 保存 `auth_token` 并将 Companion 状态置为 `active`

> 注：浏览器不直连 Companion；浏览器只调用 OpenFocus API。OpenFocus 负责在控制面做鉴权、审计与状态机。

### Browser 节点可信绑定与系统悬浮球

系统级 `Inbox` 悬浮球必须先证明当前浏览器会话与某个本机 Companion 处在同一节点，不能只根据“存在在线 Companion”判断。OpenFocus 采用 nonce 证明绑定：

1. 浏览器点击 `Inbox` 后调用 `POST /api/float_ball/start`。
2. 若当前 browser session 尚未绑定可用 Companion，OpenFocus 生成短期一次性 nonce，并返回 `openfocus://bind?...&nonce=...`。
3. 本机 Companion 由系统协议处理器或 helper 接收到该 URL 后，通过本机 protocol socket 交给正在运行的 Companion 进程。
4. Companion 使用既有 gRPC 长连接向 OpenFocus 回传 `BrowserBindProof(nonce, companion_id)`。
5. OpenFocus 校验 nonce 未过期、Companion 已配对且在线后，写入 `browser_companion_bindings`。
6. 只有绑定的 Companion 在线且声明 `system_float_ball` 能力时，OpenFocus 才下发 `FloatBallStartRequest`；否则浏览器回退到页面级悬浮球。

Companion 不为浏览器暴露 HTTP 服务；自定义协议只负责把本机 nonce 送到 Companion，后续认证与命令仍走白名单 gRPC。

## 安全模型（必须）

风险：如果 Companion 连接到“假的 OpenFocus”，等价于把本机能力交给攻击者。

最小安全闭环建议：

1) **TLS + 强校验**：Companion 必须校验 OpenFocus 的服务端证书（生产环境推荐 mTLS）。
2) **Enrollment 配对**：首次接入需要人确认（一次性配对码/短期 token），配对后下发长期设备凭证（客户端证书或长 token）。
3) **命令白名单 + 沙箱**：
   - `spawn_agent` 仅允许 `coco/codex` 等受控命令
   - `cwd` 必须在允许的 workspace 根目录内（防止任意目录执行）
   - 文件访问默认只读，限制文件大小与类型
4) **审计与追溯**：所有命令/结果/输出都写入 OpenFocus `events`，同时进入 audit memory；Companion 本地可选保留辅助日志。
5) **高危动作二次确认（可选）**：写文件/执行外部命令/打开敏感目录等需要本机弹窗确认或策略开关。

## 远程终端（Remote Terminal）

目标：在 AgentSpace 的右侧终端区域提供“可交互终端”，让用户直接在远端工作机（Companion 所在机器）的 workspace 中运行命令。

### 架构分层

- 浏览器：渲染终端（xterm.js），只通过 OpenFocus Web API 通信。
- OpenFocus（Control Plane）：
  - 负责终端 session 的创建/关闭/鉴权
  - 将浏览器的输入/resize 转发给 Companion
  - 将 Companion 的输出通过 WebSocket 复用到浏览器
- Companion（Data Plane）：
  - 为每个 terminal session 启动一个 PTY（shell），并持续读取 PTY 输出
  - 接收输入写入 PTY master fd，并支持窗口大小 resize

### 协议与数据格式

- Control Plane ⇄ Data Plane：复用 12.3 的 gRPC 双向流，在 protobuf 中增加 Terminal 相关消息：
  - `TerminalStart/Stop/Input/Resize/ListSessions`
  - `TerminalOutput(terminal_id, data, closed, error)`
- 浏览器 ⇄ OpenFocus：
  - HTTP：列出/新建/关闭终端（按 AgentSpace 维度管理）
  - WebSocket：实时转发输入/输出
  - 终端数据为二进制，WebSocket 侧采用 `base64` 放入 JSON 字段（`data_b64`）

### 终端生命周期与清理策略

- 创建：用户点击 `+`（或页面首次进入且无终端时自动创建）→ OpenFocus `POST /api/agent_spaces/{space_id}/terminals/new` → gRPC `TerminalStart` → 返回 `terminal_id`。
- 交互：浏览器通过 `WS /api/agent_spaces/{space_id}/terminals/{terminal_id}/ws` 发送：
  - `{"type":"input","data_b64":"..."}`
  - `{"type":"resize","cols":..,"rows":..}`
  OpenFocus 将其转为 gRPC `TerminalInput/TerminalResize`。
- 输出：Companion 推送 `TerminalOutput` 到 OpenFocus，OpenFocus 将其广播到订阅该 `terminal_id` 的 WebSocket 客户端。
- 关闭：用户点击 tab 上的 `x` → OpenFocus `POST /api/agent_spaces/{space_id}/terminals/{terminal_id}/close` → gRPC `TerminalStop`。
- 释放：AgentSpace 被释放时，OpenFocus 需尽力对该空间下所有 `terminal_id` 执行 `TerminalStop`，并删除 OpenFocus 侧终端记录；Companion 离线时允许仅清理 OpenFocus 侧。

## Runtime Hook Receiver

Companion 在本机额外监听 Unix domain socket：

```text
~/.openfocus/hooks.sock
```

多 OpenFocus 实例同时运行时，每个实例必须使用独立的 `OPENFOCUS_INSTANCE_ID`
与 hook socket / spool dir。默认实例仍使用 `~/.openfocus/hooks.sock`；命名实例推荐使用：

```text
~/.openfocus/hooks-<OPENFOCUS_INSTANCE_ID>.sock
```

同时每个实例有一个文件队列 fallback：

```text
/tmp/openfocus-agent-hooks-<uid>/<OPENFOCUS_INSTANCE_ID>/
```

OpenFocus service、Companion 与 `scripts/install_agent_hooks.py` 必须在读取
`OPENFOCUS_INSTANCE_ID` 前加载 repo-root `.env`。推荐把实例级配置固化到 `.env`：

```dotenv
OPENFOCUS_INSTANCE_ID=dev
OPENFOCUS_PORT=8001
OPENFOCUS_GRPC_PORT=17891
OPENFOCUS_SERVER_GRPC_ADDR=127.0.0.1:17891
```

加载规则：

- `OPENFOCUS_ENV_FILE` 可指向显式 env 文件；否则先尝试启动 cwd 的 `.env`，再尝试 repo-root `.env`。
- 已存在的 process env 优先级高于 `.env`，`.env` 不覆盖已有变量。
- `OPENFOCUS_DOTENV=0` / `false` / `off` / `no` 禁用 `.env` 自动加载。
- 同一 OpenFocus 实例的 service、Companion 与 hook installer 必须使用同一个
  `OPENFOCUS_INSTANCE_ID`、hook socket 与 spool dir；多个实例需要不同 instance id
  和不同 gRPC/Web 端口。

hook shim 必须先尝试 socket，失败后将 envelope 以 `.json` 文件原子写入 spool
dir。Companion 轮询该目录并用同一条 `AgentRuntimeSignal` 路径转发，保证 socket
临时不可用时仍能接收 runtime signals。

由 OpenFocus 启动的 AgentSpace terminal/agent 必须注入：

- `OPENFOCUS_INSTANCE_ID`
- `OPENFOCUS_HOOK_SOCK`
- `OPENFOCUS_HOOK_SPOOL_DIR`
- `OPENFOCUS_TASK_ID`
- `OPENFOCUS_TERMINAL_ID` 或 `OPENFOCUS_AGENT_SESSION_ID`

hook shim 会把 `OPENFOCUS_INSTANCE_ID` 写入 envelope。Companion 收到 signal
时必须先校验该 origin instance id；若不等于自身实例 id，则直接忽略该事件。
这允许多个 OpenFocus 实例都注册 hook，但只有启动该 agent 的实例会接受 runtime
activity。

该 socket 只用于本机 hook 回调，不暴露 HTTP 端口。OpenFocus 提供 Codex/Coco hook shim：

- `openfocus/hooks/openfocus-codex-hook.sh`
- `openfocus/hooks/openfocus-coco-hook.sh`

Codex `~/.codex/hooks.json` installation must use Codex CLI config event keys:
`SessionStart`、`UserPromptSubmit`、`PermissionRequest`、`Stop`。这些 key
和 app-server/UI 输出里的 lower-camel 或 wire 名称不是同一个配置层；installer
不得只写 `sessionStart` / `userPromptSubmit` / `permissionRequest`，否则 Codex
TUI 不会按预期执行 default OpenFocus hook。`Stop` 映射为
`runtime.turn.completed`，用于 turn 结束后提醒。
Codex 安装 hook 后还必须由用户在 Codex TUI 中运行 `/hooks`，并显式 trust
OpenFocus default hook entries；未 trust 前 Codex 不会把 hook events 发送给
OpenFocus。

hook shim 行为：

- socket 不存在或连接失败时退出 0，不影响 agent runtime。
- 读取 stdin JSON，补充 cwd/tty/ppid/terminal/task/session 等上下文。
- 将 envelope 发送到 Companion socket；发送失败时落盘到
  `OPENFOCUS_HOOK_SPOOL_DIR`，若 spool 也失败则仍退出 0，并尽力写入
  `/tmp/openfocus-agent-hooks.log` 便于诊断。
- Companion 通过 gRPC `AgentRuntimeSignal` 转发给 Core。

Core 负责将 raw signal 归一到 `runtime.turn.started`、`runtime.turn.waiting_for_approval`、`runtime.turn.waiting_for_input`、`runtime.turn.completed` 等内部事件；外部 `/api/agent/events` 不参与当前状态推断。
