# MAVUL 设计总览

> 状态：第一条 Device `Orchestrator → Reverser` 链路已实现
> 更新日期：2026-09-04

## 项目目标

MAVUL 用 Agent 自动研究软件漏洞。长期流程是
`reverse → hunt → reproduce`，当前只完成第一步，并聚焦 Cisco IOS XE /
ASA 一类运行设备。

Target 是研究对象，Device 是 Target 的一项资产。一个 Target 可以有多个
Device；一次 Reverse Task 只占用其中一个。这个区分让以后加入源代码资产时，
不用改写设备模型。

## 当前可运行流程

1. 用户准备 IDA MCP，并在其中打开要分析的二进制。
2. `main.py` 启动确定性 Orchestrator 和 Reverser。
3. 用户提交包含 Target、Device、二进制路径、目标和 Telnet 凭据的 JSON。
4. Orchestrator 生成任务 UUID；Reverser 申请独占设备租约。
5. Codex Worker 同时使用 IDA MCP 和 PTY Telnet 分析。
6. Worker 记录设备操作，独立 Reviewer 检查 Behavior Model 和证据。
7. Reviewer 批准后，同一 Worker 撤销改动并退出 Telnet。
8. Reverser 释放设备，输出摘要和 Behavior Model 路径。

设备忙时任务等待，不会提前创建 Codex Worker。系统还支持查询、中断、恢复和
直接停止。

## 代码结构

```text
main.py
system/
  agent_system.py
  agent_runtime.py
  mailbox.py
  envelope.py
  device.py
  device_runtime.py
  config.py
agents/
  orchestrator.py
  envelope_handler.py
  reverser/
  hunter.py
  reproducer.py
tests/
```

## 文档

- [领域词汇](../CONTEXT.md)
- [AgentSystem](agent-system-design.md)
- [Reverser](reverser-design.md)
- [Codex SDK 迁移](codex-migration.md)
- [ADR：Target 与 Asset 分开](adr/0001-separate-targets-from-assets.md)
- [ADR：可信运行环境](adr/0002-assume-a-trusted-runtime.md)
- [ADR：Reverser 可以修改设备](adr/0003-let-reverser-modify-its-device.md)

## 下一步

先用真实 Cisco 设备和准备好的 IDA MCP 跑通一次 Reverse Task。确认这条链路
稳定后，再实现 Hunter、Reproducer、Threat Model 和 LLM Orchestrator。
