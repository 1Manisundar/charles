"""Agent contract.

An agent turns a context into a result. It never mutates run state, never
writes files directly and never decides whether its own output is acceptable:
the engine applies its change set through the journalled workspace and the
exit gates judge the outcome.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

from ..models import NodeSpec
from ..policy import Policy, ProjectProfile
from ..providers.base import Provider
from ..schemas import ChangeSet

if TYPE_CHECKING:
    from ..store import RunStore


@dataclass
class AgentContext:
    spec: NodeSpec
    store: RunStore  # read-only by convention; the engine owns all writes
    inputs: dict[str, Any]  # latest content of every artifact the node declares
    feedback: list[str]  # human answers, change requests, gate failures, test output
    invocation: int
    provider: Provider
    policy: Policy
    profile: ProjectProfile

    @property
    def requirement(self) -> str:
        return self.store.state.requirement


@dataclass
class AgentResult:
    artifacts: dict[str, Any] = field(default_factory=dict)
    changeset: ChangeSet | None = None
    decisions: list[dict[str, Any]] = field(default_factory=list)  # title, rationale, alternatives, actor
    model: str | None = None
    usage: dict[str, int] = field(default_factory=dict)


class Agent(Protocol):
    def run(self, ctx: AgentContext) -> AgentResult: ...
