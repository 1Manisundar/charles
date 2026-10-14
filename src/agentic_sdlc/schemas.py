"""Typed contracts for what each agent must produce.

Agents are never trusted to return free text: every model-backed agent is
bound to one of these schemas, the provider validates against it, and exit
gates then check the *content* (coverage, traceability, scope). The schemas
avoid open-ended maps so they can be used as strict structured-output schemas.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

Level = Literal["low", "medium", "high"]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --------------------------------------------------------------------------- requirements


class Requirement(Strict):
    id: str = Field(description="Stable id, e.g. FR-1 or NFR-2")
    statement: str
    acceptance_criteria: list[str]
    priority: Literal["must", "should", "could"] = "must"


class OpenQuestion(Strict):
    id: str = Field(description="e.g. Q1")
    question: str
    why_it_matters: str
    options: list[str]
    recommended: str = Field(description="The option assumed if the human does not answer")


class Assumption(Strict):
    id: str
    statement: str
    source: Literal["analyst", "human"] = "analyst"


class RequirementSpec(Strict):
    title: str
    summary: str
    scenario_type: Literal["greenfield", "brownfield", "ambiguous"]
    functional: list[Requirement]
    non_functional: list[Requirement] = Field(default_factory=list)
    open_questions: list[OpenQuestion] = Field(default_factory=list)
    assumptions: list[Assumption] = Field(default_factory=list)
    out_of_scope: list[str] = Field(default_factory=list)

    def requirement_ids(self) -> list[str]:
        return [r.id for r in self.functional + self.non_functional]


# --------------------------------------------------------------------------- design


class Component(Strict):
    name: str
    path: str
    responsibility: str
    change: Literal["new", "modified", "unchanged"]


class ApiChange(Strict):
    method: str
    path: str
    summary: str
    change: Literal["new", "modified", "unchanged", "removed"]


class DataChange(Strict):
    entity: str
    change: str
    requires_migration: bool = False


class DesignDecision(Strict):
    id: str = Field(description="e.g. ADR-1")
    title: str
    context: str
    decision: str
    alternatives: list[str]
    consequences: str


class Risk(Strict):
    id: str
    description: str
    likelihood: Level
    impact: Level
    mitigation: str


class Coverage(Strict):
    requirement_id: str
    approach: str


class Design(Strict):
    overview: str
    components: list[Component]
    api: list[ApiChange] = Field(default_factory=list)
    data_model: list[DataChange] = Field(default_factory=list)
    impacted_modules: list[str] = Field(
        default_factory=list, description="Existing files this change touches (brownfield)"
    )
    decisions: list[DesignDecision]
    risks: list[Risk]
    requirement_coverage: list[Coverage]
    rollback_plan: str
    limitations: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------------- plan


class Task(Strict):
    id: str = Field(description="e.g. T1")
    title: str
    description: str
    kind: Literal["code", "migration", "config"] = "code"
    requirement_ids: list[str]
    depends_on: list[str] = Field(default_factory=list)
    writes: list[str] = Field(description="Every file this task may create or modify")


class Plan(Strict):
    summary: str
    test_strategy: str
    tasks: list[Task]


# --------------------------------------------------------------------------- change sets


class Edit(Strict):
    search: str = Field(description="Exact text that occurs once in the current file")
    replace: str


class FileChange(Strict):
    path: str = Field(description="Workspace-relative POSIX path")
    action: Literal["write", "edit", "delete"]
    content: str | None = None
    edits: list[Edit] = Field(default_factory=list)


class ChangeSet(Strict):
    rationale: str
    changes: list[FileChange]

    def paths(self) -> list[str]:
        return [c.path for c in self.changes]
