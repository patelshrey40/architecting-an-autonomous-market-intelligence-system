from __future__ import annotations

from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class SwarmDefinition:
    workflow_type: str
    display_name: str
    roles: tuple[str, ...]
    tool_whitelist: tuple[str, ...]
    max_depth: int
    max_worker_fanout: int
    max_retries: int
    max_tool_calls: int
    timeout_seconds: int
    handler: Callable


SWARM_REGISTRY: dict[str, SwarmDefinition] = {}


def register_swarm(definition: SwarmDefinition):
    SWARM_REGISTRY[definition.workflow_type] = definition
    return definition


def get_swarm_definition(workflow_type: str) -> SwarmDefinition | None:
    return SWARM_REGISTRY.get(workflow_type)
