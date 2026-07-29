import copy
import tomllib
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Config(Mapping[str, Any]):
    """Validated entry point for parsed system configuration."""

    _data: dict[str, Any]
    source: Path | None = None

    @classmethod
    def from_toml(cls, path: str | Path) -> "Config":
        source = Path(path)
        with source.open("rb") as config_file:
            return cls.from_mapping(tomllib.load(config_file), source=source)

    @classmethod
    def from_mapping(
        cls,
        data: Mapping[str, Any],
        source: Path | None = None,
    ) -> "Config":
        copied = copy.deepcopy(dict(data))
        agents = copied.get("agents")
        if not isinstance(agents, Mapping):
            raise ValueError("configuration must contain an [agents] table")
        devices = copied.get("devices", {})
        if not isinstance(devices, Mapping):
            raise TypeError("configuration 'devices' must be a table")
        return cls(copied, source)

    def agent(self, name: str) -> Mapping[str, Any]:
        agents = self._data["agents"]
        try:
            config = agents[name]
        except KeyError:
            raise LookupError(f"missing agent configuration: {name}") from None
        if not isinstance(config, Mapping):
            raise TypeError(f"agent configuration '{name}' must be a table")
        return config

    @property
    def devices(self) -> Mapping[str, Any]:
        return self._data.get("devices", {})

    def __getitem__(self, key: str) -> Any:
        return self._data[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._data)

    def __len__(self) -> int:
        return len(self._data)
