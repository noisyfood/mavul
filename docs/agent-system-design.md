# AgentSystem 设计记录

> 状态：已接通确定性 Orchestrator 与 Device Reverser
> 更新日期：2026-09-04

## 职责

AgentSystem 只负责运行和连接各部分：

- 注册主 Agent；
- 按收件人投递 Envelope；
- 限制每个主 Agent 的任务并发；
- 发送查询、中断、恢复和停止消息；
- 管理设备租约与 FIFO 等待队列。

它不分析二进制，也不判断 Behavior Model 是否正确。当前 Orchestrator 是简单
的确定性 JSON 路由器；以后加入 LLM 时，也不改变 AgentSystem 的职责。

## 入口

`main.py` 加载 TOML，然后注册 Orchestrator 和 Reverser。入口持续读取一行一个
JSON 对象。Orchestrator 支持：

| action | 行为 |
| --- | --- |
| `reverse` | 生成 UUID，输出 `accepted`，向 Reverser 提交任务 |
| `query` | 查询任务状态或当前进度 |
| `interrupt` | 中断指定任务 |
| `resume` | 恢复暂停或阻塞的任务 |
| `stop` | 输出 `stopped`，直接停止系统 |

输入由单独的 daemon 线程读取，因此 Orchestrator 调用 `stop()` 后，主循环不会
再次卡在 `input()`。

## Envelope

Agent 间只传三种 Envelope：

- `TaskEnvelope`：任务正文和结构化信息；
- `ResultEnvelope`：状态、短摘要和产物路径；
- `SystemEnvelope`：查询、中断、恢复、停止和设备通知。

`envelope_id` 是当前进程内的 UUID，用来忽略重复消息。`task_id` 标识用户
任务，`correlation_id` 关联一次请求和回复。当前没有消息持久化或结果 ACK。

## 线程与并发

每个主 Agent 有一个邮箱、一个任务线程池和一个查询线程。普通任务按
`max_concurrency` 运行，多出的任务按 FIFO 等待。

`QUERY_TASK` 使用独立查询线程。`RESUME_TASK` 和 `DEVICE_AVAILABLE` 使用
任务线程池，因为两者都可能继续执行 Codex Worker。其他 SystemEnvelope 在邮箱
线程中快速处理，这样中断不会排在长任务后面。

设备忙时，Reverser 的初次处理只保存 `waiting_for_device` 状态并立即返回，
不会打开 Codex thread。设备空闲通知到达后，真正的逆向工作才占用任务线程。

## 设备租约

Device 配置包含 ID、名称、IP 和可选的 Telnet、SSH、串口地址。当前真实流程
使用 `ip_address` 和 `telnet_port`。

一个 Device 同时只有一个租约。租约所有者是 `(agent_id, task_id)`，所以同一
Agent 的不同任务不会共享设备。设备忙时，新请求进入 FIFO：

```text
task A holds Device
        |
task B waits -> task C waits
        |
A releases -> B gets DEVICE_AVAILABLE -> C remains waiting
```

等待任务可以按完整所有者取消。释放设备时，DeviceRuntime 先把租约授给下一个
任务，再向该任务发送带 `task_id` 的 `DEVICE_AVAILABLE`。Reverser 还会处理
“已经授予但中断先到”的竞态：迟到通知不会启动已暂停任务，而是释放该租约。

设备 baseline、隔离和 Orchestrator 抢占的基础代码仍保留，但不在第一次真实
reverse 的正常路径中。

## 停止

`stop()` 创建一个协调线程，依次停止 Agent，等待它们的线程池退出，再关闭
AgentSystem 自己的控制线程池。从 Agent 自己的线程调用时，`stop()` 立即返回
一个只读完成对象，避免线程等待自己；外部调用会等待停止结束。

系统级 stop 不要求 Reverser 先撤销设备改动。Reverser 会中断活动 turn、关闭
任务会话和 Codex App Server；其中运行的 Telnet 进程随之退出。

## 源码地图

| 文件 | 内容 |
| --- | --- |
| `system/agent_system.py` | 注册、路由、入口循环和停止 |
| `system/agent_runtime.py` | 每个主 Agent 的邮箱和并发 |
| `system/mailbox.py` | System 优先的两级 FIFO |
| `system/envelope.py` | 三种 Envelope 和 SystemAction |
| `system/device.py` | Device、租约、等待队列和 baseline |
| `system/device_runtime.py` | 设备操作和通知 |
| `agents/orchestrator.py` | JSON 请求到 Envelope 的确定性映射 |
| `agents/reverser/` | 逆向任务、Codex Worker、审阅和设备生命周期 |

## 当前限制

- 进程重启后，内存中的设备租约不会自动恢复或核对。
- Reverser 尚未接入设备抢占和隔离后的完整恢复流程。
- Hunter、Reproducer、Recovery Agent 和 LLM Orchestrator 尚未实现。
