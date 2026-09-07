# Codex SDK 迁移记录

> 状态：Reverser 已使用 `openai-codex==0.147.0`
> 更新日期：2026-09-04

## 边界

Codex SDK 负责模型执行：

- 创建和恢复持久 thread；
- 运行和中断 turn；
- 派生只读查询 thread；
- 创建独立的只读 Reviewer；
- 提供 shell、PTY 和 MCP。

MAVUL 自己负责任务状态、Envelope 路由、并发和设备租约。SDK 不替代这些
功能。

## 当前实现

`agents/reverser/execution.py` 定义项目使用的窄接口，`codex.py` 适配 SDK，
`codex_config.py` 把 TOML 转成 thread 选项。领域代码不直接操作 SDK 类型。

每个 Reverse Task 使用一个持久 Worker thread。分析被 Reviewer 退回时，继续
同一个 thread；查询使用派生的只读 thread，不把查询提示写进 Worker 历史。
Reviewer 每轮使用新的只读 thread，并返回结构化的 `approve`、`revise` 或
`blocked`。

Worker 使用 `Sandbox.workspace_write`，网络始终开启，以便直接运行
`/usr/bin/telnet`。配置的 IDA MCP 同时提供给 Worker。Reviewer 和查询 thread
不使用 Worker 的 MCP。

SDK 的高层 Python 接口没有项目内自定义工具回调，因此设备交互没有另做 MCP
或 helper。Codex App Server 自带的 shell 支持 PTY 和连续写入 stdin，足以让
每个 Worker 保持自己的 Telnet 进程。官方参考：
[App Server](https://learn.chatgpt.com/docs/app-server)。

## 恢复

`task-state.json` 保存不透明的 Codex thread ID。第一次 turn 真正开始后，
`session_initialized` 变为 true：

- 空且从未执行过的 thread 丢失时，可以创建新 thread；
- 已经执行过的 thread 丢失时，任务报告 `blocked`，不假装恢复成功；
- 进程重启时，遗留活动任务转为 `paused`，由用户发出 `resume`。

Worker thread 丢失后，Reverser 可以按 thread ID 恢复。Telnet 进程如果还在，
继续使用；如果已经丢失，Worker 重新连接。

## 工作目录

```text
<task-id>/
  task-state.json
  progress.md
  analysis/behavior-model.md
  delegated/manifest.json
  work/
    evidence/
    analysis/
    delegated/
```

Worker 的 cwd 是 `work/`。证据审阅只列出 `work/evidence/` 中的真实文件
相对路径，然后让只读 Reviewer 自己打开。这里没有证据哈希、快照或大小扫描。

`codex.home` 保存 Codex 登录状态，目录不存在时会自动创建。首次运行前，用同一
路径完成 Codex 登录。例如：

```bash
CODEX_HOME=/absolute/path/to/mavul/memory/codex codex login --device-auth
```

## 尚未做

- Hunter 和 Reproducer 还没有使用这套执行接口。
- 进程重启后的设备租约与真实设备状态还没有自动核对。
- Codex RPC 没有项目自定义超时；App Server 完全失联时，关闭仍可能等待。
