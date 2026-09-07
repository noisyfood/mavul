import argparse
from pathlib import Path
from typing import Sequence

from agents.orchestrator import register as register_orchestrator
from agents.reverser import register as register_reverser
from system.agent_system import AgentSystem
from system.config import Config


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Start the vulnerability discovery system."
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("config.toml"),
        help="path to the system TOML configuration (default: config.toml)",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    config = Config.from_toml(args.config)
    system = AgentSystem(
        config,
        registrars=(register_reverser, register_orchestrator),
    )
    try:
        system.serve()
    finally:
        system.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
