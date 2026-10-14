"""The seam between agents and whatever produces their reasoning.

An agent describes *what* it needs (role instructions, context payload, output
schema); a provider decides *how* it is produced. Swapping the provider swaps
a live model for a deterministic replay without touching orchestration,
gates or policy - which is what makes the control plane testable offline.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from pydantic import BaseModel


@dataclass
class ProviderRequest:
    role: str
    node_id: str
    instructions: str
    payload: dict[str, Any]
    schema: type[BaseModel]
    invocation: int  # 0 for the node's first result, 1 for the next, ...


@dataclass
class ProviderResponse:
    output: BaseModel
    model: str
    usage: dict[str, int] = field(default_factory=dict)


class Provider(Protocol):
    name: str

    def generate(self, request: ProviderRequest) -> ProviderResponse:
        """Return schema-valid output or raise Transient/AgentOutput/PermanentError."""
        ...
