# Repository Guidelines

## Project Structure & Module Organization

This repository is an early-stage Python agent system for vulnerability discovery. `main.py` is the intended
entry point and owns configuration loading and system construction. Core runtime
coordination belongs in `system/` (`agent_system.py`, `condenser.py`), while
specialized agent implementations live in `agents/` (`orchestrator.py`,
`hunter.py`, `reproducer.py`, and the `reverser/` package). Keep reusable
orchestration logic out of `main.py`. The ignored `memory/` directory is reserved
for runtime state; do not commit generated state or secrets there.

Tests live under `tests/`; mirror source responsibilities when adding coverage,
for example `tests/test_reverser_codex.py`.

## Build, Test, and Development Commands

The project has no packaging or build configuration. Use Python 3.12.

- `python -m compileall main.py agents system` checks all modules for syntax
  errors.
- `python -m unittest discover -s tests -p "test_*.py"` runs the offline
  standard-library test suite.
- `python main.py --config config.toml` invokes the local entry point. Configure
  Codex, IDA MCP, and a Device before running a real Reverse Task; Hunter and
  Reproducer are still placeholders.

`agents/reverser/codex.py` integrates `openai-codex`; keep its dependency pin and
example configuration synchronized with adapter changes.

## Coding Style & Naming Conventions

Follow PEP 8 with four-space indentation. Use `snake_case` for modules,
functions, methods, and variables; use `PascalCase` for classes such as
`AgentSystem`; and use `UPPER_SNAKE_CASE` for constants. Add type hints to
public functions and concise docstrings to classes or behavior that is not
self-evident. Keep imports grouped as standard library, third-party, then local.
Avoid placeholder `pass` statements in completed changes.

## Testing Guidelines

Use `unittest` unless the repository adopts another framework explicitly.
Name files `test_<module>.py` and methods `test_<behavior>`. Cover configuration
validation, agent routing, failure paths, and external-tool boundaries. Mock LLM
and filesystem integrations so tests remain deterministic and offline.

## Commit & Pull Request Guidelines

No Git history is present in this checkout, so no existing commit convention can
be inferred. Use short, imperative subjects such as `Implement TOML config
loading`, and keep unrelated changes separate. Pull requests should explain the
motivation and behavior, list validation commands run, link relevant issues,
and call out new dependencies or configuration. Include logs or screenshots
only when they clarify user-visible behavior.

## Security & Configuration

Never commit API keys, tokens, local configuration, or generated memory. Read
secrets from environment variables and provide sanitized example configuration
when a config format is introduced.

## Core Design

This repo is designed for auto vulnerability discovery. In early design, we consider
the pipeline as "reverse-hunt-reproduce". The target should be any software system,
but in early stage we just aim at IoT system especially Cisco IOS XE / ASA system.

## Coding Principle

- Do NOT add unneccessary redundancy code which will damage simplicity and elegent code.
- Propose design or pseudocode first. Edit code base after user agree with your plan.
- Start a subagent who act as a professional developer in vulnerability and agent system
  design. He will propose his advice when he notice some improper coding or design.
