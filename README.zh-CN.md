# codex_client

[English](README.md)

许可证：[MIT](LICENSE)

本仓库包含 `codex_ws_client.py`，这是一个通过 WebSocket 连接 `codex app-server` 的轻量级客户端。

脚本位于 `skills/codex-ws-client/scripts/codex_ws_client.py`。

它的首要使用场景是在 Claude Code 中运行，让 Claude 模型通过一个持续连接的 `codex app-server` 与 Codex 协作。

## 演示

https://github.com/user-attachments/assets/2c8af159-df13-4d09-ae32-cb2a96eb1fe7

如果无法内联播放，请直接打开 [brainstorm.mp4](brainstorm.mp4)。

它适用于需要完成以下任务的代理或脚本：

- 向正在运行的 Codex app-server 发送提示词
- 通过 `--thread-id` 复用已持久化的线程
- 流式输出或缓冲输出助手响应
- 获取机器可读的 JSON 输出
- 在同一连接上通过 REPL 模式反复发送提示
- 通过 stderr 日志或 NDJSON 跟踪查看更多服务端行为

## 作为 Skill 安装

这个仓库已经将客户端按 skill 目录结构打包在 `skills/codex-ws-client/` 下。

项目级安装（仅对当前项目可用）：

```powershell
Copy-Item -Recurse -Force skills/codex-ws-client .codex/skills/codex-ws-client
```

全局安装（对所有项目可用）：

```powershell
Copy-Item -Recurse -Force skills/codex-ws-client $HOME/.codex/skills/codex-ws-client
```

项目级安装后，从对应路径运行客户端：

```powershell
python .codex/skills/codex-ws-client/scripts/codex_ws_client.py --json "Summarize this repo"
```

全局安装后，改用 `$HOME/.codex/skills/codex-ws-client/scripts/codex_ws_client.py`。

## 适用场景

在以下情况下使用这个脚本：

- 你希望 Claude Code 将任务委托给 Codex，或继续使用同一个 Codex 线程
- 已经有一个长期运行的 `codex app-server`
- 你希望比每次都启动 `codex exec` 拥有更低的开销
- 你希望直接控制线程 ID、超时、JSON 输出和日志

在以下情况下不建议使用：

- 你需要基于 stdio 的传输方式，而不是 WebSocket
- 你需要更大型封装工具提供的完整任务或会话编排
- 你需要在非 REPL 模式下进行完善的交互式审批

## 传输方式

这个客户端仅支持连接：

- `codex app-server --listen ws://HOST:PORT`

默认 URI：

```text
ws://127.0.0.1:8765
```

## 核心行为

客户端使用如下协议流程：

1. 连接到 WebSocket
2. 发送 `initialize`
3. 发送 `initialized`
4. 创建或恢复线程
5. 发送 `turn/start`
6. 持续消费流式通知，直到当前轮次结束

它可以处理：

- `item/agentMessage/delta`
- `turn/completed`
- `turn/failed`
- 与审批、文件变更、权限相关的服务端请求
- 部分线程、工具、命令、文件变更通知

## 线程模型

新线程：

- 如果未传入 `--thread-id`，客户端会创建新线程

恢复线程：

- 如果传入 `--thread-id`，客户端会调用 `thread/resume`
- 恢复线程后的轮次会使用 `--resume-timeout`

持久化：

- 线程默认会被持久化
- `--ephemeral` 会禁用持久化
- `--thread-id` 仅适用于非临时线程

注意：

- `--ephemeral` 线程无法跨连接恢复
- 如果恢复线程失败，单次调用模式会快速失败
- 在 REPL 模式下，某些过期线程场景可能会回退为新线程

## 输出模式

纯文本：

- 默认模式会将增量内容流式输出到 stdout

缓冲文本：

- `--no-stream` 会在轮次结束后一次性输出最终助手文本

JSON：

- `--json` 会向 stdout 输出结构化 JSON 对象
- 这是供其他 LLM 或工具消费的最佳模式

当前 JSON 结构包括：

- `thread_id`
- `turn_id`
- `status`
- `text`
- 可选的 `error`
- 可选的 `notifications`
- 可选的 `metrics`

`metrics` 当前包括：

- `latency_ms`
- `input_tokens`
- `output_tokens`

## 常用命令

单次提示：

```powershell
python skills/codex-ws-client/scripts/codex_ws_client.py "Summarize this repo"
```

供工具使用的 JSON 输出：

```powershell
python skills/codex-ws-client/scripts/codex_ws_client.py --json "List the main entrypoints"
```

复用已持久化线程：

```powershell
python skills/codex-ws-client/scripts/codex_ws_client.py --thread-id THREAD_ID "Continue the previous conversation"
```

交互式 REPL：

```powershell
python skills/codex-ws-client/scripts/codex_ws_client.py --repl --print-thread-id
```

带交互式审批的 REPL：

```powershell
python skills/codex-ws-client/scripts/codex_ws_client.py --repl --interactive-approvals
```

从文件读取提示：

```powershell
python skills/codex-ws-client/scripts/codex_ws_client.py --prompt-file prompt.txt
```

带跟踪的结构化输出：

```powershell
python skills/codex-ws-client/scripts/codex_ws_client.py --json --ndjson-file trace.jsonl "Return metadata"
```

## REPL 命令

REPL 模式下可用：

- `/thread` 打印当前线程 ID
- `/new` 创建新线程
- `/exit` 或 `/quit` 退出 REPL

## 日志与调试

详细级别：

- `-v` 会将生命周期事件和部分通知摘要输出到 stderr
- `-vv` 会将原始 JSON-RPC 流量输出到 stderr

跟踪文件：

- `--ndjson-file FILE` 会以 JSON Lines 形式追加 JSON-RPC 流量

摘要：

- `--summary` 会将 token 用量和延迟输出到 stderr

保存最终消息：

- `--out FILE` 会将最终助手文本写入文件

## 审批处理

默认行为：

- 命令审批默认自动拒绝
- 文件变更审批默认自动拒绝
- 权限请求默认拒绝

REPL 覆盖行为：

- `--interactive-approvals` 会启用基于提示的处理方式，用于：
  - 命令审批
  - 文件变更审批
  - 权限请求

当前仍不支持：

- 服务端请求的动态工具执行
- 简单审批提示之外的工具用户输入请求
- ChatGPT 身份验证令牌刷新请求

对于不支持的服务端请求，客户端会显式返回响应，而不是直接忽略。

## 超时

`--timeout`

- 常规 WebSocket 消息等待超时

`--connect-timeout`

- 初始连接超时

`--resume-timeout`

- 恢复线程后发送轮次时使用的超时

将任意超时设置为 `0` 表示不限制超时。

## 退出码

- `0`：成功
- `1`：轮次失败
- `2`：参数错误
- `3`：连接失败
- `4`：超时
- `5`：JSON 或 schema 解析错误
- `130`：被中断

## 面向其他 LLM 的最佳实践

推荐：

- 使用 `--json` 供机器消费
- 如果只需要最终文本，使用 `--no-stream`
- 仅对已知已持久化线程使用 `--thread-id`
- 调试协议行为时使用 `--ndjson-file`

避免：

- 将 `--thread-id` 用于通过 `--ephemeral` 创建的线程
- 在单次调用模式中依赖仅限 REPL 的特性
- 假设客户端完整覆盖所有服务端请求类型

推荐的单次调用模式：

```powershell
python skills/codex-ws-client/scripts/codex_ws_client.py --json --connect-timeout 10 --timeout 120 "YOUR PROMPT"
```

推荐的恢复线程模式：

```powershell
python skills/codex-ws-client/scripts/codex_ws_client.py --json --thread-id THREAD_ID --resume-timeout 300 "YOUR PROMPT"
```

## 已知限制

- 仅支持 WebSocket，不支持 stdio 模式
- 采用单进程 CLI 设计，不是可复用库
- 不是完整的协议框架
- 在 Windows 上，对进行中轮次的优雅中断支持仍有限
- 对更复杂的服务端请求族只做了部分处理，并不完整

## 与 app-server 的关系

这个脚本是一个客户端。

它不会自动启动服务端。

使用前，你必须先运行类似下面的命令：

```powershell
codex app-server --listen ws://127.0.0.1:8765
```

然后再使用本客户端。
