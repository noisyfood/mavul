import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from openhands.sdk import Agent, AgentContext, Conversation, LLM, Tool

from agents.reverser_types import (
    MAIN_SYSTEM_PROMPT,
    WORKER_SYSTEM_PROMPT,
    ReverseTask,
)


class OpenHandsRuntime:
    """Build OpenHands agents and persistent conversations from configuration."""

    def __init__(self, config: Mapping[str, Any]) -> None:
        self.config = dict(config)

    def create_reviewer(self, workspace: Path) -> Any:
        return Conversation(
            agent=self.build_agent("main", tools=[]),
            workspace=workspace,
            visualizer=None,
            delete_on_close=False,
        )

    def create_worker(self, task: ReverseTask) -> Any:
        return Conversation(
            agent=self.build_agent("worker", self.worker_tools()),
            workspace=task.workspace,
            persistence_dir=task.workspace / "conversations",
            conversation_id=task.conversation_id,
            visualizer=None,
            delete_on_close=False,
        )

    def build_agent(self, role: str, tools: list[Tool]) -> Agent:
        config = self._role_config(role)
        default_prompt = MAIN_SYSTEM_PROMPT if role == "main" else WORKER_SYSTEM_PROMPT
        tool_limit = self._positive_int(
            config.get("tool_concurrency_limit", 1),
            f"{role}.tool_concurrency_limit",
        )
        return Agent(
            llm=self._build_llm(role),
            tools=tools,
            mcp_config=self._mcp_config() if tools else {},
            agent_context=AgentContext(system_message_suffix=default_prompt),
            tool_concurrency_limit=tool_limit,
        )

    @staticmethod
    def worker_tools() -> list[Tool]:
        try:
            from openhands.tools.file_editor import FileEditorTool
            from openhands.tools.task_tracker import TaskTrackerTool
            from openhands.tools.terminal import TerminalTool
        except (ImportError, ModuleNotFoundError) as exc:
            raise RuntimeError(
                "OpenHands SDK and tools must use matching pinned versions"
            ) from exc
        return [
            Tool(name=TerminalTool.name),
            Tool(name=FileEditorTool.name),
            Tool(name=TaskTrackerTool.name),
        ]

    def _build_llm(self, role: str) -> LLM:
        config = self._role_config(role)
        model = str(self._required(config, "model"))
        options = config.get("options", {})
        if not isinstance(options, Mapping):
            raise TypeError(f"Reverser configuration '{role}.options' must be a table")
        llm_options = dict(options)
        reserved = {"api_key", "base_url", "model", "usage_id"}
        invalid = reserved.intersection(llm_options)
        if invalid:
            names = ", ".join(sorted(invalid))
            raise ValueError(f"reserved LLM options for role '{role}': {names}")

        auth_type = config.get("auth_type", "api_key")
        if auth_type == "subscription":
            return LLM.subscription_login(
                vendor=str(config.get("vendor", "openai")),
                model=model,
                **llm_options,
            )
        if auth_type != "api_key":
            raise ValueError(f"unsupported auth_type for Reverser role '{role}'")

        api_key_env = str(config.get("api_key_env", "LLM_API_KEY"))
        return LLM(
            model=model,
            api_key=os.getenv(api_key_env),
            base_url=config.get("base_url"),
            usage_id=f"reverser-{role}",
            **llm_options,
        )

    def _role_config(self, role: str) -> dict[str, Any]:
        config = self._required(self.config, role)
        if not isinstance(config, Mapping):
            raise TypeError(f"Reverser configuration '{role}' must be a table")
        return dict(config)

    def _mcp_config(self) -> dict[str, Any]:
        config = self.config.get("mcp_config", {})
        if not isinstance(config, Mapping):
            raise TypeError("Reverser configuration 'mcp_config' must be a table")
        servers = config.get("mcpServers", config)
        if not isinstance(servers, Mapping):
            raise TypeError("Reverser MCP server configuration must be a table")
        return dict(servers)

    @staticmethod
    def _required(config: Mapping[str, Any], key: str) -> Any:
        value = config.get(key)
        if value is None or value == "":
            raise ValueError(f"missing Reverser configuration: {key}")
        return value

    @staticmethod
    def _positive_int(value: object, name: str) -> int:
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            raise ValueError(f"{name} must be a positive integer")
        return value
