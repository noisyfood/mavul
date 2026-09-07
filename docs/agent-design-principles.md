# Agent 设计原则

> 状态：基于 AgentSystem 与 Reverser 首轮实现总结
> 更新日期：2026-09-04

## 目标

本文规定 MAVUL Agent 的稳定接口、职责归属和可选内部结构。当前不建立承载
所有能力的 `BaseAgent`：Reverser 是专业任务 Agent 的参考实现，而不是
Orchestrator、Recovery Agent 或嵌套 Agent 的共同父类。公共设计采用小接口
和组合式模块；只有出现第二个真实实现后，才提取更深的公共模块。

## Agent 类型

| 类型 | 消息与生命周期 |
| --- | --- |
| Orchestrator | 接收用户输入和 `ResultEnvelope`，拥有全局任务决策与最终编排权 |
| 专业主 Agent | 如 Reverser、Hunter、Reproducer；接收任务和系统指令，判断局部交付物是否就绪 |
| Recovery Agent | 接收恢复任务，租用设备、执行恢复并报告；是否恢复仍由 Orchestrator 决定 |
| 嵌套 Agent | 没有主邮箱；由父 Agent 管理，只向 AgentSystem 注册唯一资源身份与控制回调 |

现有 `EnvelopeHandler` 只适用于受 Orchestrator 调度的专业主 Agent，不能强制
Orchestrator 或嵌套 Agent 继承。

## 职责归属

| 所有者 | 通用职责 |
| --- | --- |
| Orchestrator | 选择 Agent、拆分全局任务、验收全局结果、决定抢占、恢复和停机 |
| AgentSystem | 注册、路由、邮箱优先级、并发准入、等待队列、去重、设备租约和异常上报 |
| 专业主 Agent | 解释领域任务、管理执行单元、保证线程安全、判断局部完成并形成结果 |
| 执行 Adapter | 封装 Codex thread/turn、模型、sandbox、MCP、事件和错误 |

AgentSystem 只决定消息何时、向谁投递以及资源是否可用；它不解释任务内容，
也不判断专业结果。Agent 不重复实现主邮箱、跨 Agent 路由或设备等待队列。
跨 Agent 信息一律使用 Envelope；基础设施能力通过注入的接口调用。

## 主 Agent 最小接口

每个主 Agent 提供包级 `register(agent_system, config)` 作为组合根。注册函数
构造 Agent，并向 AgentSystem 声明：

```python
agent_system.register_agent(
    name=agent.agent_id,
    receiver=agent.receive,
    accepted_envelopes=(...),
    max_concurrency=agent.max_concurrency,
    stop=agent.stop,
)
```

AgentSystem 只依赖以下行为：

- `receive(envelope) -> None`：处理已路由消息；结果通过
  `AgentSystem.submit()` 回传，而不是作为返回值传递。
- `stop() -> None`：幂等地拒绝新任务、中断并保存活动任务、等待执行单元
  静止，再关闭私有资源。
- 稳定且唯一的 `agent_id`、正整数 `max_concurrency` 和明确的可接收
  Envelope 类型。

`start_task()`、`query_task()`、`resume_task()` 等是 Agent 私有领域接口，由
其 Handler 调用，不属于 AgentSystem seam。

## 标准内部结构

| 模块 | 适用范围 | 职责 |
| --- | --- | --- |
| `registration.py` | 所有主 Agent | 读取配置、注入依赖并注册最小接口 |
| `agent.py` | 所有主 Agent | 主门面、执行单元所有权和领域协调 |
| `handler.py` | 有邮箱的 Agent | Envelope 授权、动作分派、请求关联和结果构造 |
| `models.py` | 按需 | 任务状态、结果和可预期请求异常 |
| `execution.py` | LLM Agent 按需 | 定义项目拥有的执行会话与审阅窄接口 |
| `codex.py` | Codex Agent 按需 | 实现 thread 创建/恢复、turn 中断及会话隔离 |
| `codex_config.py` | Codex Agent 按需 | 校验模型、sandbox、网络和 MCP 配置并转换成 SDK 选项 |
| `sessions.py` | 会话恢复较复杂时 | 会话创建、恢复、初始化点和活动 turn 生命周期 |
| `prompts.py` | LLM Agent 按需 | 保存角色提示词和结构化审阅约束 |
| `workspace.py` | 有持久产物时 | 状态写入、进度、索引和产物管理 |
| `state.py` | 状态较复杂时 | 合法转换、内存/磁盘一致提交和失败回滚 |
| `reviewer.py` | 需要本地验收时 | 机械校验和领域专家审阅 |
| `delegation.py` | 允许嵌套探索时 | 子 ID、子工作区、manifest 和递归控制 |
| `control.py` | 生命周期较复杂时 | 协作式中断、保存现场与资源关闭 |

这些是标准槽位，不是必须为每个 Agent 创建的空文件。实现很小时应留在
`agent.py`；只有隐藏了真实复杂性时才拆成独立模块。

## 通用运行不变量

- 任务范围内的 `TaskEnvelope`、`SystemEnvelope` 和 `ResultEnvelope` 必须保留
  全局 `task_id`；系统级控制可以省略它。回复使用 `correlation_id` 关联原请求。
- 确定性代码负责控制动作、状态转换和设备租约；LLM 只承担领域推理与专业
  判断。
- 不同任务可能并行，System 指令还可能绕过任务线程池；Agent 必须保护共享
  状态，并对同一任务的执行、查询、中断和恢复作出明确串行化规定。
- 中断是协作式过程：先阻止进一步副作用，再保存停止点和下一步。`stop()`
  必须等待这一过程完成，不能假设线程池能够安全强杀任务。
- 预期的非法请求返回相关联的 error Result；工具或 SDK 失败进入可恢复的
  `blocked`；内部代码错误仍上报 AgentSystem。
- 活动 turn 的中断必须等待 SDK 报告该 turn 已结束，之后才能持久化最终
  停止状态或释放租约；仅设置本地标志不能证明副作用已经停止。
- 交付前先持久化结果及产物索引，再提交 Result。当前 submit 成功仅表示
  路由器接受消息，不代表 Orchestrator 已验收；设计有意暂不采用 ACK。
- 主 `agent_id`、全局 `task_id`、Codex thread ID、嵌套资源 ID 和
  `envelope_id` 含义不同，不得混用。设备租约必须属于实际操作设备的
  最底层执行身份。
- 任务到 thread 的映射必须在首次 turn 前持久化，并在 `thread.turn()` 成功
  后记录可恢复历史已经初始化；任务状态和交付账本由宿主持有，不能只从
  模型会话历史推断。

## Agent 自行设计的部分

每个领域 Agent 必须明确以下策略，而不是依赖通用父类猜测：

- TaskEnvelope 内容的解释方式，以及任务状态、转换和终止条件；
- 系统提示词、模型角色、工具、MCP 与网络或设备能力；
- 何为完整、未知但已尽力、blocked、failed 或可交付；
- 交付物内容、证据标准、产物路径和摘要格式；
- 是否需要 Reviewer、打回循环或多角色 thread；
- Workspace 布局、恢复时保留什么、重建什么；
- 是否创建嵌套 Agent，以及拆分、递归停止和 manifest 语义；
- 设备初始化、操作日志、抢占后保存和恢复到 baseline 的流程。

## Reverser 映射

Reverser 的 `agent.py` 拥有 `ReverseTask` 和 run-review-revise 状态机；
`handler.py` 把 Envelope 映射到任务操作；`execution.py` 定义执行 seam，
`codex.py` 是生产 Adapter，`codex_config.py` 转换配置，测试使用 Fake；
`workspace.py` 管理证据和行为模型；`reviewer.py` 实现逆向专家验收；
`delegation.py` 在宿主目录保存嵌套身份与 manifest。

行为模型五要素、事实关联证据、`APPROVE/REVISE/BLOCKED`、IDA/GDB 工具、
`evidence/` 和 `analysis/behavior-model.md` 均为 Reverser 领域策略，不能进入
通用 Agent 父类。顶层任务持久化 Codex thread ID，而嵌套执行当前按已确认
设计由 manifest 与子工作区重构；这也不是所有 Agent 的统一规则。

## 抽象与测试原则

公共模块的接口必须比其实现更小，并隐藏调用者不应知道的细节。执行后端是
真实 seam：生产只使用 Codex Adapter，测试使用内存 Fake。不要为已经放弃
的 OpenHands 保留兼容分支；这会扩大接口并迫使领域代码理解两套生命周期。
注册、Envelope 关联、并发安全和幂等停止应通过公共接口做契约测试；状态机、
审阅门槛和产物格式则由各 Agent 独立测试。

在 Hunter 或 Reproducer 完成前，不提取统一 `TaskLifecycle`、状态枚举、
Reviewer、Delegation 或 Workspace 父类。第二个实现出现真实重复后，再用
删除测试判断抽象是否成立：删除公共模块若会让相同复杂性散落回多个 Agent，
该模块才具有足够深度。
