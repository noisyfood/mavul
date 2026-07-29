from collections.abc import Mapping
from typing import Any

from system.envelope import SystemEnvelope, TaskEnvelope


def register(agent_system: Any, config: Mapping[str, Any]) -> Any:
    """Create and register the Reverser main Agent with AgentSystem."""
    from agents.reverser import Reverser

    agents = config.get("agents")
    if not isinstance(agents, Mapping):
        raise ValueError("configuration must contain an [agents] table")
    reverser_config = agents.get("reverser")
    if not isinstance(reverser_config, Mapping):
        raise ValueError("configuration must contain an [agents.reverser] table")

    reverser = Reverser(
        reverser_config,
        submit=agent_system.submit,
        register_child=agent_system.register_child_agent,
        unregister_child=agent_system.unregister_child_agent,
    )
    agent_system.register_agent(
        "reverser",
        reverser.receive,
        (TaskEnvelope, SystemEnvelope),
        reverser.max_concurrency,
        reverser.stop,
    )
    return reverser
