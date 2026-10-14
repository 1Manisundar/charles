"""The explicit dependency graph the orchestrator executes.

The graph is static for the SDLC stages and grows at runtime: once a plan is
approved-for-review, its tasks become real nodes under the ``implementation``
join node, with the plan's own dependency edges. Scheduling, invalidation and
rollback order are all derived from this one structure.
"""

from __future__ import annotations

from .models import NodeSpec, NodeStatus


class GraphError(ValueError):
    pass


class WorkflowGraph:
    def __init__(self, static_nodes: list[NodeSpec]):
        self._static = {n.id: n for n in static_nodes}
        self._dynamic: dict[str, NodeSpec] = {}
        self.validate()

    # ------------------------------------------------------------------ structure

    @property
    def nodes(self) -> dict[str, NodeSpec]:
        return {**self._static, **self._dynamic}

    def __getitem__(self, node_id: str) -> NodeSpec:
        return self._dynamic.get(node_id) or self._static[node_id]

    def __contains__(self, node_id: str) -> bool:
        return node_id in self._static or node_id in self._dynamic

    def set_dynamic(self, specs: dict[str, NodeSpec]) -> None:
        previous = self._dynamic
        self._dynamic = dict(specs)
        try:
            self.validate()
        except GraphError:
            self._dynamic = previous
            raise

    def children(self, node_id: str) -> list[str]:
        return [n.id for n in self._dynamic.values() if n.parent == node_id]

    def deps(self, node_id: str) -> list[str]:
        """Effective dependencies: a join node also waits for all its children."""
        spec = self[node_id]
        return list(dict.fromkeys(spec.depends_on + (self.children(node_id) if spec.join else [])))

    def inputs(self, node_id: str) -> list[str]:
        """Artifacts a node consumes: a join node also consumes its children's outputs."""
        spec = self[node_id]
        extra = [out for child in self.children(node_id) for out in self[child].outputs] if spec.join else []
        return list(dict.fromkeys(spec.inputs + extra))

    def dependents(self, node_id: str) -> list[str]:
        return [n for n in self.nodes if node_id in self.deps(n)]

    def descendants(self, node_id: str) -> list[str]:
        """Everything transitively downstream, in topological order."""
        seen: set[str] = set()
        stack = [node_id]
        while stack:
            for dep in self.dependents(stack.pop()):
                if dep not in seen:
                    seen.add(dep)
                    stack.append(dep)
        return [n for n in self.topological_order() if n in seen]

    def ancestors(self, node_id: str) -> set[str]:
        seen: set[str] = set()
        stack = [node_id]
        while stack:
            for dep in self.deps(stack.pop()):
                if dep not in seen:
                    seen.add(dep)
                    stack.append(dep)
        return seen

    def topological_order(self) -> list[str]:
        indegree = {n: len(self.deps(n)) for n in self.nodes}
        ready = sorted(n for n, d in indegree.items() if d == 0)
        order: list[str] = []
        while ready:
            current = ready.pop(0)
            order.append(current)
            for dep in sorted(self.dependents(current)):
                indegree[dep] -= 1
                if indegree[dep] == 0:
                    ready.append(dep)
        if len(order) != len(indegree):
            raise GraphError(f"dependency cycle among: {sorted(set(indegree) - set(order))}")
        return order

    def validate(self) -> None:
        nodes = self.nodes
        for spec in nodes.values():
            for dep in spec.depends_on:
                if dep not in nodes:
                    raise GraphError(f"{spec.id} depends on unknown node {dep!r}")
            if spec.parent and spec.parent not in nodes:
                raise GraphError(f"{spec.id} has unknown parent {spec.parent!r}")
            if spec.rework and spec.rework.target not in nodes:
                raise GraphError(f"{spec.id} reworks unknown node {spec.rework.target!r}")
        self.topological_order()

    # ------------------------------------------------------------------ rendering

    def to_mermaid(self, statuses: dict[str, NodeStatus] | None = None) -> str:
        """Mermaid flowchart of the graph, optionally coloured by run status."""
        statuses = statuses or {}

        def ident(node_id: str) -> str:
            return node_id.replace(":", "_").replace("-", "_")

        lines = ["flowchart TD"]
        for node_id in self.topological_order():
            spec = self[node_id]
            marks = []
            if spec.approval:
                marks.append(f"human: {spec.approval.checkpoint}")
            if spec.join:
                marks.append("join")
            label = spec.title + ("<br/><i>" + ", ".join(marks) + "</i>" if marks else "")
            shape = f'{{{{"{label}"}}}}' if spec.join else f'["{label}"]'
            lines.append(f"    {ident(node_id)}{shape}")
        for node_id in self.topological_order():
            for dep in self.deps(node_id):
                lines.append(f"    {ident(dep)} --> {ident(node_id)}")
        for node_id, spec in self.nodes.items():
            if spec.rework:
                lines.append(f"    {ident(node_id)} -. rework .-> {ident(spec.rework.target)}")
        palette = {
            NodeStatus.SUCCEEDED: "#d4edda",
            NodeStatus.FAILED: "#f8d7da",
            NodeStatus.AWAITING_APPROVAL: "#fff3cd",
            NodeStatus.RUNNING: "#cce5ff",
            NodeStatus.STALE: "#e2e3e5",
        }
        for node_id, status in statuses.items():
            if node_id in self and status in palette:
                lines.append(f"    style {ident(node_id)} fill:{palette[status]},color:#000")
        return "\n".join(lines)
