"""Agent registry: the named roles a workflow node can be bound to."""

from __future__ import annotations

from .base import Agent, AgentContext, AgentResult
from .codebase import CodebaseAnalyst
from .docs import DocWriter, TemplateDocWriter
from .model_agents import Architect, Implementer, Planner, RequirementsAnalyst
from .release import Promoter, ReleaseManager, SyncJoin
from .verifier import SecurityReviewer, Verifier


def default_agents() -> dict[str, Agent]:
    return {
        "requirements_analyst": RequirementsAnalyst(),
        "codebase_analyst": CodebaseAnalyst(),
        "architect": Architect(),
        "planner": Planner(),
        "implementer": Implementer(),
        "sync_join": SyncJoin(),
        "verifier": Verifier(),
        "security_reviewer": SecurityReviewer(),
        "doc_writer": DocWriter(),
        "doc_template": TemplateDocWriter(),
        "release_manager": ReleaseManager(),
        "promoter": Promoter(),
    }


__all__ = ["Agent", "AgentContext", "AgentResult", "default_agents"]
