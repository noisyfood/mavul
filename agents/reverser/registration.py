from collections.abc import Mapping
from typing import Any

from system.envelope import SystemEnvelope, TaskEnvelope

from .agent import Reverser
from .codex import CodexExecutionBackend


def register(agent_system: Any, config: Mapping[str, Any]) -> Any:
    """Create and register the Reverser main Agent with AgentSystem."""
    agents = config.get("agents")
    if not isinstance(agents, Mapping):
        raise ValueError("configuration must contain an [agents] table")
    reverser_config = agents.get("reverser")
    if not isinstance(reverser_config, Mapping):
        raise ValueError("configuration must contain an [agents.reverser] table")

    backend = CodexExecutionBackend(reverser_config)
    try:
        reverser = Reverser(
            reverser_config,
            agent_system.devices,
            submit=agent_system.submit,
            register_child=agent_system.register_child_agent,
            unregister_child=agent_system.unregister_child_agent,
            execution_backend=backend,
        )
        agent_system.register_agent(
            reverser.agent_id,
            reverser.receive,
            (TaskEnvelope, SystemEnvelope),
            reverser.max_concurrency,
            reverser.stop,
        )
    except Exception:
        backend.close()
        raise
    return reverser
