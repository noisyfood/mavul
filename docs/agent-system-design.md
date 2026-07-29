# AgentSystem 设计记录

> 状态：首轮运行时实现  
> 更新日期：2026-07-29

## 职责边界

`Orchestrator` 拥有所有任务和灾难处理决策。`AgentSystem` 只维护运行
环境、线程、邮箱、路由、资源租约和生命周期，不选择执行 Agent，也不
判断任务是否完成。

`main.py` 解析 TOML 为 `Config`，将配置实例及 Agent 注册函数交给
`AgentSystem`。当前只接入 Reverser；Orchestrator 和 Recovery Agent
仍待实现，因此入口尚不能形成完整用户交互服务。

## Envelope 与路由

所有 Agent 间消息继承 `Envelope`，公共头包括：

- `envelope_id`、`envelope_type` 和带时区的 `created_at`；
- `sender`、`recipient`；
- 可选 `task_id` 和 `correlation_id`。

具体类型为：

- `TaskEnvelope`：任务内容及必要信息；
- `ResultEnvelope`：Agent 签名、状态、结论摘要及产物索引；
- `SystemEnvelope`：事件、结构化 action、详细信息及参数。

Agent 调用 `AgentSystem.submit()` 投递消息，系统只依据 `recipient`
路由。每个主 Agent 只有一个邮箱，分 system、agent 两级；system 优先，
同级 FIFO。重复 `envelope_id` 在当前进程内只执行一次。

## 线程与并发

每个主 Agent 注册一个最大并发数。普通 Envelope 进入有界
`ThreadPoolExecutor`，达到上限后由 AgentSystem 的运行时队列等待。
`SystemEnvelope`
由独立邮箱调度线程处理，不占任务并发槽，因此满载时仍可执行查询、
中断和停止。

嵌套 Agent 没有邮箱。主 Agent 使用唯一 ID 注册其中断回调；
`AgentSystem` 可将 `SystemEnvelope` 直接投递至该 ID，且嵌套 Agent
不计入顶层并发配额。结束后必须注销。

## 设备租约

每个设备由独立 `Device` 实例维护：

- 租约完全独占，并使用 lease UUID 与 generation 防止旧租约误释放；
- 等待队列 FIFO，释放后自动授予并通知下一 Agent；
- Orchestrator 可强制抢占，原持有者进入等待队列首位；
- 状态未知时进入 `quarantined`；仅 Recovery Agent 可在隔离期间取得
  受限租约，恢复成功后只有 Orchestrator 可解除隔离；
- 只有配置的 Recovery Agent 持有有效租约时才能更新 baseline；
- 其他 Agent 恢复设备前读取最新 baseline。

设备连接信息来自配置，凭证只保存环境变量名称。Baseline 使用按设备
ID 索引的原子 JSON 文件。

## 中断与停机

单任务中断与整个 Agent 中断使用不同 `SystemEnvelope` action。
OpenHands `Conversation.pause()` 先请求在步骤边界停止，运行线程静止后
再写入进度。Agent 内部 Hook 负责总结和保存现场；AgentSystem 不解析
这些业务状态。

最终停机由 Orchestrator 在所有 Agent 完成中断后调用。AgentSystem
停止接收新任务、丢弃未开始任务、等待运行线程退出、调用 Agent 的
stop callback，最后关闭控制执行器。

## 当前实现边界

- Orchestrator、Recovery Agent 及具体设备连接适配器尚未实现。
- Agent 自动重启、持久化投递账本和跨进程 Envelope 去重尚未实现。
- 嵌套 Agent 已有 ID 注册和 manifest，但尚无满足并行及恢复要求的
  delegation tool。
- Workspace 仍是路径约定，不是操作系统级安全隔离。
- 当前消息确认语义止于 `submit()` 成功，不包含 Result ACK；这是已确认
  的首轮边界。
