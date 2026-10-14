"""Deterministic provider that replays a playbook of recorded agent outputs.

A playbook is a directory::

    playbook.json          requirement, per-node response sequence, scripted approvals, faults
    responses/*.json       structured outputs (requirement spec, design, plan)
    changesets/<name>/     file trees; ``x.py`` is written, ``x.py.edit`` holds
                           SEARCH/REPLACE blocks, ``x.py.delete`` deletes

A node that runs more than once (after a clarification, a change request or a
rework round) consumes the next response in its sequence, so a playbook can
express "first draft, then the revision".
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from ..models import AgentOutputError, PermanentError
from .base import ProviderRequest, ProviderResponse

_EDIT_BLOCK = re.compile(r"<<<<<<< SEARCH\n(.*?)\n=======\n(.*?)>>>>>>> REPLACE", re.DOTALL)


def _read(path: Path) -> str:
    # Normalise line endings so a checkout with CRLF conversion replays identically.
    return path.read_bytes().decode("utf-8").replace("\r\n", "\n")


def parse_edit_blocks(text: str) -> list[dict[str, str]]:
    edits = [
        {"search": search, "replace": replace[:-1] if replace.endswith("\n") else replace}
        for search, replace in _EDIT_BLOCK.findall(text)
    ]
    if not edits:
        raise PermanentError("edit file contains no SEARCH/REPLACE blocks")
    return edits


def load_changeset_dir(root: Path, rationale: str) -> dict[str, Any]:
    if not root.is_dir():
        raise PermanentError(f"playbook change set not found: {root}")
    changes = []
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        rel = path.relative_to(root).as_posix()
        if rel.endswith(".edit"):
            changes.append({"path": rel[: -len(".edit")], "action": "edit", "edits": parse_edit_blocks(_read(path))})
        elif rel.endswith(".delete"):
            changes.append({"path": rel[: -len(".delete")], "action": "delete"})
        else:
            changes.append({"path": rel, "action": "write", "content": _read(path)})
    return {"rationale": rationale, "changes": changes}


class Playbook:
    def __init__(self, root: Path):
        self.root = root
        self.config: dict[str, Any] = json.loads(_read(root / "playbook.json"))

    @property
    def name(self) -> str:
        return self.config["name"]

    @property
    def requirement(self) -> str:
        return _read(self.root / self.config["requirement_file"]).strip()

    @property
    def approvals(self) -> list[dict[str, Any]]:
        return list(self.config.get("approvals", []))

    @property
    def faults(self) -> list[dict[str, Any]]:
        return list(self.config.get("faults", []))

    def response(self, node_id: str, invocation: int) -> dict[str, Any]:
        sequence = self.config.get("responses", {}).get(node_id)
        if not sequence:
            raise PermanentError(f"playbook '{self.name}' has no recorded response for node '{node_id}'")
        entry = sequence[min(invocation, len(sequence) - 1)]
        if isinstance(entry, str):
            return json.loads(_read(self.root / entry))
        if "changeset" in entry:
            return load_changeset_dir(self.root / entry["changeset"], entry.get("rationale", ""))
        return entry


class ScriptedProvider:
    name = "scripted"

    def __init__(self, playbook: Playbook):
        self.playbook = playbook

    def generate(self, request: ProviderRequest) -> ProviderResponse:
        raw = self.playbook.response(request.node_id, request.invocation)
        try:
            output = request.schema.model_validate(raw)
        except ValidationError as exc:
            raise AgentOutputError(f"recorded output for {request.node_id} violates its schema: {exc}") from exc
        return ProviderResponse(output=output, model=f"playbook:{self.playbook.name}")
