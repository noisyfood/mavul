# Reverser 设计记录

> 状态：首轮实现  
> 更新日期：2026-07-29

## 职责

`Reverser` 是常驻、LLM 驱动的主 Agent。它只接收任务、创建执行单元、
维护工作区索引、审阅结果并向 Orchestrator 交付。每个全局任务 ID 对应
一个顶层子 Agent 和一棵持久化 Conversation；主 Agent 认为结果不清晰时，
必须在同一 Conversation 中退回修改。

顶层子 Agent 负责绘制局部程序地图，至少覆盖入口点、攻击者可控输入、
检查与转换、前置状态，以及可疑点附近逻辑。事实必须关联证据；可疑点
可以保持推测，不要求漏洞结论。穷尽合理手段后仍未知的事项由主 Agent
判断是否允许结束。

## 工作区与状态

每个任务的逻辑写入边界是 `<workspace>/<task-id>/`：

- `evidence/` 保存反汇编、调试记录等证据；
- `analysis/` 保存行为模型等分析产物；
- `delegated/` 保存嵌套子 Agent 的 manifest 与子工作区；
- `conversations/` 保存 OpenHands Conversation；
- `progress.md` 保存中断点，`task-state.json` 保存可恢复的内部状态。

主 Agent 维护根目录 `index.md`，记录任务目标、状态和结论摘要。中断会
等待运行线程静止后再保存进度；恢复顶层任务时沿用相同 Conversation
ID，并附加 Orchestrator 的恢复注意事项。最终行为模型保存为
`analysis/behavior-model.md`，不会整份塞入索引。

## OpenHands SDK 映射

实现使用 `Agent`、`Conversation` 和 `AgentContext`。
`Conversation.pause()` 处理中断，固定 UUID 与 `persistence_dir` 支持
恢复，`ask_agent()` 用于不改变任务历史的状态查询和主 Agent 审阅。
执行 Agent 可使用终端、文件编辑、任务追踪和 MCP 工具。审阅输入包含
证据路径、大小、SHA-256 和有界文本摘录，且行为模型必须引用现存证据。

## 已知边界

`receive()` 现已接收 `TaskEnvelope` 与 `SystemEnvelope`，并通过
AgentSystem 的提交接口回传关联的 `ResultEnvelope`。查询、单任务中断、
整个 Reverser 中断、恢复和停止均使用结构化 system action。

嵌套 Agent 已支持唯一 ID 注册、直接中断回调及 manifest/subworkspace
持久化；具体创建仍由顶层子 Agent负责。由于尚无符合并行和重构语义的
delegation tool，本轮没有让 OpenHands 自动创建嵌套 Agent。

设备租约、FIFO 等待、抢占、隔离和 baseline 已由 AgentSystem 提供，
但尚未接入 OpenHands 自定义设备工具，也没有具体协议适配器。因此当前
提示词仍禁止 Worker 直接访问测试设备。

OpenHands `LocalWorkspace` 只设置工作目录，并不提供操作系统级写隔离。
因此“只能写入任务子目录”目前仍是行为约束，而不是安全边界；正式运行
需要容器或受限远程 Workspace。顶层并发、排队和 system 控制旁路现由
AgentSystem 的线程运行时执行。

本轮没有启用 `TaskToolSet`。当前 SDK 的该工具需要额外注册子 Agent，
同步执行且共享父 Workspace，其跨进程恢复也不足以满足已确认的嵌套
Conversation 语义。应在专用 delegation runtime 设计完成后再接入。

`requirements.txt` 将 `openhands-sdk` 与 `openhands-tools` 固定为同一
版本。运行环境必须按该文件安装；不匹配时工具层会在首次创建 Worker
时给出明确错误。

## SDK 参考

- [Getting Started](https://docs.openhands.dev/sdk/getting-started)
- [Conversation Persistence](https://docs.openhands.dev/sdk/guides/convo-persistence)
- [MCP Integration](https://docs.openhands.dev/sdk/guides/mcp)
- [Agent Settings](https://docs.openhands.dev/sdk/guides/agent-settings)
