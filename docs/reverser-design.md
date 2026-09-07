# Reverser 设计记录

> 状态：已接通确定性 Orchestrator、设备租约、Telnet Worker 和 IDA MCP
> 更新日期：2026-09-04

## 当前目标

第一阶段只做一件事：完成一次真实的设备逆向任务。用户给出 Target、Device、
一个已经在 IDA MCP 中打开的二进制和分析目标。Orchestrator 把任务交给
Reverser，Reverser 分析二进制并在设备上检查至少一个真实行为，最后交付
Behavior Model。

当前 Reverse Task 不需要 Threat Model。Threat Model 留给以后由 LLM
Orchestrator 创建 Finding 时使用。

## 输入与输出

入口读取一行一个 JSON 对象。新任务格式为：

```json
{"action":"reverse","target_id":"t1","device_id":"lab_router","binary_path":"/path/to/binary","objective":"...","username":"user","password":"pass","enable_password":"enable-pass"}
```

Orchestrator 生成 UUID，并立即输出：

```json
{"type":"accepted","task_id":"..."}
```

任务完成、暂停、等待或失败后，再输出：

```json
{"type":"result","task_id":"...","status":"...","summary":"...","artifacts":[".../analysis/behavior-model.md"]}
```

结果只包含摘要和文件路径，不在 stdout 展开整个 Behavior Model。入口还支持
`query`、`interrupt`、`resume` 和 `stop`。任务刚输出 `accepted`、尚未进入
Reverser 时，`query` 会返回 `starting`，稍后再查即可。

## 任务与设备

Target 和 Device 是两件事。Target 是研究对象，Device 是它的一项资产。
一个 Target 可以有多个 Device，但一次 Reverse Task 只使用一个 Device。
当前不建 Target 注册表；请求直接携带 `target_id`、`device_id` 和
`binary_path`。

设备地址保存在 TOML 的 `[devices.<id>]` 中：

```toml
[devices.lab_router]
name = "Lab router"
ip_address = "192.0.2.10"
telnet_port = 23
```

用户名、登录密码和 enable 密码直接来自任务 JSON，并保存在任务状态中，方便
恢复。配置和代码仓库中仍不应写入真实凭据。

设备租约由 `(agent_id, task_id)` 标识。同一个 Reverser 的两个任务不会共享
租约。设备忙时，任务进入 `waiting_for_device`，不启动 Codex Worker；设备按
FIFO 空闲后，`DEVICE_AVAILABLE` 才进入 Reverser 的任务线程池。等待中的任务
可以取消，迟到的设备通知会把误授予的租约立即释放。

## Worker 执行

每个任务拥有一个持久 Codex thread、独立工作区、独立 shell 和独立终端进程。
Worker 网络始终开启。它使用：

- IDA MCP：按 `binary_path` 选择已经打开的二进制；MAVUL 不负责启动 IDA。
- `/usr/bin/telnet IP PORT`：在 PTY 中启动，随后分次输入用户名、登录密码、
  `enable` 和 enable 密码。

Telnet 进程可以跨多轮分析和修改继续存在。IDA MCP 可以同时打开多个二进制，
因此 Reverser 并发不需要限制为 1。

如果 Worker 要修改设备，必须先创建 `work/device-changes-pending`。撤销全部改动
后才能删除它。这个标记只说明设备是否还有待撤销的改动，不是证据文件。

## Behavior Model 与审阅

Behavior Model 至少说明：

- 程序入口点；
- 外部或攻击者可控输入；
- 检查和转换；
- 数据流；
- 所需的先前状态；
- 附近值得继续检查的逻辑。

Worker 把证据写到 `work/evidence/`。设备操作统一追加到
`work/evidence/device-operations.md`，按顺序记录时间、命令、设备输出和错误。
Behavior Model 必须引用这个文件，并说明至少一个在真实设备上检查过的行为。

宿主只做三个简单检查：Worker 输出非空、证据目录至少有一个文件、输出引用了
一个真实的相对证据路径。随后，独立的只读 Reviewer 直接打开证据，返回
`approve`、`revise` 或 `blocked`。`revise` 继续同一个 Worker thread，没有
固定轮数上限。本项目不生成证据哈希或证据清单。

## 完成、暂停和失败

正常成功顺序固定为：

1. 保存已批准的 `analysis/behavior-model.md`；
2. 同一个 Worker 撤销设备改动，并把过程追加到设备操作日志；
3. Worker 退出 Telnet，删除待撤销标记，返回 `CLEANUP_COMPLETE`；
4. Reverser 关闭任务会话并释放设备租约；
5. 状态进入 `ready_to_deliver`，结果提交成功后进入 `delivered`。

清理或关闭失败时，任务进入 `blocked` 并保留租约。清理中断后进入 `paused`；
`resume` 只继续清理，不会重新分析和审阅。

分析或审阅中断时，任务保留租约和 Worker thread。恢复时继续原 thread；如果
Telnet 进程已经丢失，Worker 重新连接。运行失败且设备没有待撤销改动时，Worker
会尝试退出 Telnet，然后释放租约并报告 `blocked`。有待撤销改动时保留租约，
等待恢复。

同一运行时按收到的顺序处理 `resume` 和 `interrupt`；后到的中断不会被已经排队
但尚未执行的恢复覆盖。清理已经返回 `CLEANUP_COMPLETE` 时，任务完成优先。

系统级 `stop` 是唯一例外：它直接中断执行、关闭 Codex App Server 和 Telnet
进程并退出，不先撤销设备改动。

## 工作区

```text
<reverser-workspace>/<task-id>/
  task-state.json
  progress.md
  analysis/
    behavior-model.md
  delegated/
    manifest.json
  work/
    device-changes-pending
    evidence/
      device-operations.md
    analysis/
    delegated/
```

`task-state.json` 保存 Target、Device、二进制路径、凭据、状态和 Codex thread
ID。运行状态不写入 Git；默认工作区位于被忽略的 `memory/`。

## 当前限制

- 进程重启后还没有把内存租约与设备真实状态重新核对；恢复任务时会重新申请
  设备。这不阻挡第一次真实 reverse，但未撤销改动不会自动恢复。
- 设备抢占和隔离的基础消息仍在，Reverser 当前只把正常等待、释放和任务中断
  接入主流程。
- Hunter、Reproducer 和 LLM Orchestrator 仍是后续工作。
