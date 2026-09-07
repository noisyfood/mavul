# Agent Runtime 框架对照（已取代）

> 状态：历史决策记录。此前“继续使用 OpenHands”的结论已被
> [Codex SDK 迁移设计](codex-migration.md) 取代。
> 更新日期：2026-09-04

## 当前结论

MAVUL 的生产执行内核统一迁移到 Codex Python SDK，不再维持 OpenHands
Adapter。Codex 负责 LLM thread/turn、模型调用、sandbox、MCP、事件和结构化
输出；项目自身继续负责领域监督和跨 Agent 控制。

这不是 AgentSystem 的替换。Codex SDK 通过 App Server 操作单个或一组执行
thread，但没有本项目按接收者路由的 Envelope、System 优先邮箱、每类 Agent
配额、全局任务账本、设备租约/抢占/quarantine，以及 Orchestrator 有序停机
协议。官方能力边界参见 [Codex SDK](https://learn.chatgpt.com/docs/codex-sdk)
和 [App Server](https://learn.chatgpt.com/docs/app-server)。

## 三层职责

| 层 | 当前归属 | Codex SDK 关系 |
| --- | --- | --- |
| 执行内核 | `agents/reverser/codex.py` | 直接使用：thread、turn、中断、sandbox、MCP、结构化输出 |
| 领域监督 | `agents/reverser/agent.py` | 项目实现：逆向状态机、证据审阅、打回与交付 |
| 系统控制面 | `system/agent_system.py`、`agent_runtime.py` | 项目实现：路由、配额、排队、重启、停机和资源租约 |

生产代码通过 `execution.py` 的窄接口访问执行内核，测试使用 Fake。这样 SDK
生命周期与错误只集中在一个 Adapter，Agent 领域代码无需理解 App Server
协议，也无需为旧 OpenHands 生命周期保留分支。

## 能力取舍

| 需求 | Codex SDK | MAVUL 决策 |
| --- | --- | --- |
| 持久任务会话 | 支持 thread 创建与恢复 | 持久化 `task_id ↔ thread_id`，领域状态另存 |
| 即时任务中断 | 支持中断活动 turn | 保留 turn handle，确认结束后保存状态和释放资源 |
| 不污染主会话的查询 | 支持读取和派生 thread | 使用只读临时观察会话，不向 Worker 注入查询 |
| 专家审阅 | 支持独立 thread 与结构化输出 | 每次新建只读 Reviewer，直接打开证据文件 |
| 外部工具 | 支持 shell、PTY 和 MCP | IDA 使用 MCP，设备使用任务自己的 Telnet 进程 |
| 文件和命令 | 提供 workspace sandbox 配置 | Worker 网络开启，只写自己的任务目录 |
| Agent 间消息 | 不提供 MAVUL Envelope 语义 | 保留 Envelope、Mailbox 与 sender 校验 |
| 主 Agent 并发与排队 | 不提供 MAVUL 配额语义 | 保留 AgentRuntime |
| 设备独占与恢复 | 不提供 | 保留 AgentSystem、Recovery Agent 和 baseline |

Codex 的 sandbox 不管理设备归属。Worker 只有在 AgentSystem 已经把设备租给
该任务后才启动。Envelope 由确定性代码构造；LLM 负责分析，不负责路由。

## 不再采用的方案

旧版文档曾建议让 OpenHands Conversation 承担 Reverser Worker/Reviewer，
并评估 TaskToolSet、DelegateTool、AutoGen、Microsoft Agent Framework 和
LangGraph。该比较用于解释“执行内核不能替代控制平面”，但不再代表依赖
选择。完全迁移后不保留双 Adapter，也不为了框架可移植性扩大执行接口。

旧 OpenHands conversation UUID 与事件历史不会转换成 Codex thread。迁移时
保留来源字段，把遗留活动任务标记为 `paused`，再由 Orchestrator 决定是否
从 manifest、进度和工作区重建。

## 仍然成立的设计约束

1. AgentSystem 是唯一 Envelope 路由、并发准入和设备租约真相源。
2. Reverser 决定局部逆向结果是否清晰；Orchestrator 决定全局下一步。
3. 任务领域状态、证据索引和交付状态不能只存在于 Codex thread 历史中。
4. 嵌套 thread 只有接入层级 ID、工作区和设备租约后，才能独立使用设备。
5. SDK/App Server 故障作为 `blocked` 返回；如果设备已有待撤销改动，任务
   保留租约等待恢复。
