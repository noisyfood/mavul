import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from openai_codex import ApprovalMode, CodexConfig, Sandbox


class CodexRuntimeConfig:
    """Validate Reverser config and translate it to Codex SDK options."""

    _EFFORTS = {"none", "minimal", "low", "medium", "high", "xhigh"}

    def __init__(self, config: Mapping[str, Any]) -> None:
        self._config = dict(config)
        self._roles = {
            name: self._role_config(name) for name in ("main", "worker")
        }
        servers = self._config.get("mcp_servers", {})
        if not isinstance(servers, Mapping):
            raise TypeError("Reverser configuration 'mcp_servers' must be a table")
        self._mcp_servers = dict(servers)

    def client_config(self) -> CodexConfig:
        raw = self._config.get("codex", {})
        if not isinstance(raw, Mapping):
            raise TypeError("Reverser configuration 'codex' must be a table")
        overrides = raw.get("config_overrides", ())
        if not isinstance(overrides, (list, tuple)) or not all(
            isinstance(item, str) for item in overrides
        ):
            raise TypeError("codex.config_overrides must be an array of strings")
        experimental = raw.get("experimental_api", False)
        if not isinstance(experimental, bool):
            raise TypeError("codex.experimental_api must be a boolean")
        codex_bin = raw.get("codex_bin")
        if codex_bin is not None and not isinstance(codex_bin, str):
            raise TypeError("codex.codex_bin must be a string")
        home = raw.get("home")
        if isinstance(home, str) and home:
            home_path = Path(home).expanduser()
        else:
            raise ValueError("missing Reverser configuration: codex.home")
        home_path = self._prepare_home(home_path)
        runtime_path = self._runtime_binary(codex_bin)
        return CodexConfig(
            codex_bin=str(runtime_path) if runtime_path is not None else None,
            config_overrides=tuple(overrides),
            env={"CODEX_HOME": str(home_path)},
            experimental_api=experimental,
        )

    @staticmethod
    def _prepare_home(home_path: Path) -> Path:
        """Create the configured Codex home before SDK startup."""
        try:
            home_path.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise ValueError(f"cannot create codex.home: {home_path}") from exc
        if not home_path.is_dir():
            raise ValueError("codex.home must be a directory")
        return home_path.resolve(strict=True)

    @staticmethod
    def _runtime_binary(codex_bin: str | None) -> Path | None:
        if codex_bin is None:
            return None
        runtime = Path(codex_bin).expanduser()
        try:
            runtime = runtime.resolve(strict=True)
        except OSError as exc:
            raise ValueError(f"Codex binary does not exist: {runtime}") from exc
        if not runtime.is_file() or not os.access(runtime, os.X_OK):
            raise ValueError(f"Codex binary is not executable: {runtime}")
        return runtime

    def thread_options(
        self,
        role_name: str,
        workspace: Path,
        instructions: str,
        sandbox: Sandbox,
        *,
        include_tools: bool,
    ) -> dict[str, Any]:
        role = self._roles[role_name]
        options: dict[str, Any] = {
            "approval_mode": self._approval_mode(role),
            "cwd": str(workspace),
            "developer_instructions": instructions,
            "model": self._required_text(role, "model"),
            "sandbox": sandbox,
        }
        for name in ("model_provider", "service_tier"):
            value = self._optional_text(role, name)
            if value is not None:
                options[name] = value
        thread_config = self._role_thread_config(role)
        self._isolate_project_instructions(thread_config)
        if include_tools:
            self._add_worker_config(thread_config)
        else:
            thread_config["mcp_servers"] = {}
        options["config"] = thread_config
        return options

    def effort(self, role_name: str) -> str | None:
        return self._optional_text(self._roles[role_name], "effort")

    def observer_config(self) -> dict[str, Any]:
        """Disable inherited MCP servers for read-only observer forks."""
        return {
            "project_doc_max_bytes": 0,
            "project_doc_fallback_filenames": [],
            "project_root_markers": [],
            "mcp_servers": {
                name: {"enabled": False} for name in self._mcp_servers
            }
        }

    def _add_worker_config(
        self,
        thread_config: dict[str, Any],
    ) -> None:
        sandbox = thread_config.get("sandbox_workspace_write", {})
        if not isinstance(sandbox, Mapping):
            raise TypeError("worker.config.sandbox_workspace_write must be a table")
        sandbox = dict(sandbox)
        sandbox["network_access"] = True
        thread_config["sandbox_workspace_write"] = sandbox
        if self._mcp_servers:
            thread_config["mcp_servers"] = self._mcp_servers

    @staticmethod
    def _isolate_project_instructions(thread_config: dict[str, Any]) -> None:
        thread_config["project_doc_max_bytes"] = 0
        thread_config["project_doc_fallback_filenames"] = []
        thread_config["project_root_markers"] = []

    def _role_config(self, role: str) -> dict[str, Any]:
        raw = self._config.get(role)
        if not isinstance(raw, Mapping):
            raise ValueError(f"missing Reverser configuration: {role}")
        config = dict(raw)
        self._required_text(config, "model")
        self._approval_mode(config)
        self._role_thread_config(config)
        effort = self._optional_text(config, "effort")
        if effort is not None and effort not in self._EFFORTS:
            raise ValueError(f"unsupported Codex effort: {effort}")
        return config

    @staticmethod
    def _role_thread_config(role: Mapping[str, Any]) -> dict[str, Any]:
        raw = role.get("config", {})
        if not isinstance(raw, Mapping):
            raise TypeError("Reverser role 'config' must be a table")
        return dict(raw)

    @staticmethod
    def _approval_mode(role: Mapping[str, Any]) -> ApprovalMode:
        value = role.get("approval_mode", "deny_all")
        try:
            return ApprovalMode(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"unsupported Codex approval_mode: {value}") from exc

    @staticmethod
    def _required_text(config: Mapping[str, Any], name: str) -> str:
        value = config.get(name)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"missing Reverser configuration: {name}")
        return value

    @staticmethod
    def _optional_text(config: Mapping[str, Any], name: str) -> str | None:
        value = config.get(name)
        if value is None:
            return None
        if not isinstance(value, str) or not value.strip():
            raise TypeError(f"Reverser configuration '{name}' must be a string")
        return value
