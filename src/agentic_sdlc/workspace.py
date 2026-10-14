"""Sandboxed workspace: the only way agents touch files.

Agents never write to the real codebase. Each run works on a sandbox copy of
the target directory; every change is journalled with before-images so it can
be rolled back per node, and the sandbox is only *promoted* to the target after
a human release approval (with a backup so the promotion itself can be undone).
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import threading
import time
from pathlib import Path, PurePosixPath

from .models import AgentOutputError, PolicyViolation
from .schemas import ChangeSet

IGNORED_DIRS = {".git", ".venv", "venv", "__pycache__", ".pytest_cache", ".mypy_cache", "node_modules", ".sdlc"}
IGNORED_SUFFIXES = (".pyc", ".db", ".db-wal", ".db-shm", ".sqlite3")
IGNORED_FILES = {".coverage"}


TEMP_SUFFIX = ".tmp~"  # in-flight atomic writes; never part of the tree


def _ignored(rel: PurePosixPath) -> bool:
    return (
        any(part in IGNORED_DIRS for part in rel.parts)
        or rel.name in IGNORED_FILES
        or rel.name.endswith(IGNORED_SUFFIXES + (TEMP_SUFFIX,))
    )


def list_tree(root: Path) -> list[str]:
    """Sorted workspace-relative POSIX paths of every tracked file under root."""
    if not root.exists():
        return []
    out = []
    for path in root.rglob("*"):
        if path.is_file():
            rel = PurePosixPath(path.relative_to(root).as_posix())
            if not _ignored(rel):
                out.append(str(rel))
    return sorted(out)


def file_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def tree_manifest(root: Path) -> dict[str, str]:
    return {rel: file_digest(root / rel) for rel in list_tree(root)}


def _write_text(path: Path, content: str) -> None:
    # Bytes, not text mode: keeps "\n" line endings (and therefore hashes)
    # identical on every platform.
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + TEMP_SUFFIX)
    tmp.write_bytes(content.encode("utf-8"))
    # Parallel read-only stages may have the destination open for a moment; on
    # Windows that makes the rename fail, so give the reader a moment to finish.
    for attempt in range(20):
        try:
            tmp.replace(path)
            return
        except PermissionError:
            if attempt == 19:
                raise
            time.sleep(0.02)


class Workspace:
    def __init__(self, root: Path, journal_dir: Path):
        # Absolute: tools are run with the sandbox as their working directory,
        # where a path relative to the caller's directory would mean something else.
        self.root = root.resolve()
        self.journal_dir = journal_dir.resolve()
        self._lock = threading.RLock()
        self.root.mkdir(parents=True, exist_ok=True)
        self.journal_dir.mkdir(parents=True, exist_ok=True)
        (self.journal_dir / "undone").mkdir(exist_ok=True)

    # ------------------------------------------------------------------ paths

    def resolve(self, rel: str) -> Path:
        """Map an agent-supplied path into the sandbox or refuse it."""
        norm = rel.replace("\\", "/")
        pure = PurePosixPath(norm)
        if not norm or pure.is_absolute() or re.match(r"^[A-Za-z]:", norm) or ".." in pure.parts:
            raise PolicyViolation(f"path escapes the workspace: {rel!r}")
        if _ignored(pure):
            raise PolicyViolation(f"path is not writable by agents: {rel!r}")
        full = (self.root / Path(*pure.parts)).resolve()
        if self.root.resolve() not in full.parents:
            raise PolicyViolation(f"path escapes the workspace: {rel!r}")
        return full

    def files(self) -> list[str]:
        return list_tree(self.root)

    def exists(self, rel: str) -> bool:
        return self.resolve(rel).is_file()

    def read(self, rel: str) -> str:
        return self.resolve(rel).read_bytes().decode("utf-8")

    # ------------------------------------------------------------------ journalled changes

    def _journal_files(self) -> list[Path]:
        return sorted(self.journal_dir.glob("*.json"))

    def _next_seq(self) -> int:
        names = [p.name for p in self.journal_dir.rglob("*.json")]
        return max((int(n.split("__")[0]) for n in names), default=0) + 1

    def applied_owners(self) -> list[str]:
        """Owners with live (not rolled back) changes, oldest first."""
        return [json.loads(p.read_text(encoding="utf-8"))["owner"] for p in self._journal_files()]

    def has_changes(self, owner: str) -> bool:
        return owner in self.applied_owners()

    def apply(self, owner: str, changeset: ChangeSet) -> dict[str, str | None]:
        """Apply a change set atomically; returns {path: sha256 or None if deleted}.

        All changes are staged and validated first, so a bad edit leaves the
        workspace untouched. The journal is written before the files so a crash
        between the two is still recoverable.
        """
        with self._lock:
            staged: dict[str, str | None] = {}
            for change in changeset.changes:
                full = self.resolve(change.path)
                current = (
                    staged[change.path]
                    if change.path in staged
                    else (full.read_bytes().decode("utf-8") if full.is_file() else None)
                )
                if change.action == "write":
                    if change.content is None:
                        raise AgentOutputError(f"{change.path}: 'write' needs content")
                    staged[change.path] = change.content
                elif change.action == "delete":
                    staged[change.path] = None
                else:
                    if current is None:
                        raise AgentOutputError(f"{change.path}: cannot edit a file that does not exist")
                    if not change.edits:
                        raise AgentOutputError(f"{change.path}: 'edit' needs at least one edit")
                    for edit in change.edits:
                        count = current.count(edit.search)
                        if count != 1:
                            raise AgentOutputError(
                                f"{change.path}: search text must match exactly once, matched {count} times: "
                                f"{edit.search[:80]!r}"
                            )
                        current = current.replace(edit.search, edit.replace)
                    staged[change.path] = current

            before = {}
            for rel in staged:
                full = self.resolve(rel)
                before[rel] = full.read_bytes().decode("utf-8") if full.is_file() else None
            seq = self._next_seq()
            safe_owner = re.sub(r"[^A-Za-z0-9_.-]", "_", owner)
            journal = self.journal_dir / f"{seq:05d}__{safe_owner}.json"
            _write_text(journal, json.dumps({"seq": seq, "owner": owner, "before": before}))

            for rel, content in staged.items():
                full = self.resolve(rel)
                if content is None:
                    full.unlink(missing_ok=True)
                else:
                    _write_text(full, content)
            return {
                rel: (hashlib.sha256(c.encode("utf-8")).hexdigest() if c is not None else None)
                for rel, c in staged.items()
            }

    def rollback(self, owner: str) -> list[str]:
        """Undo every live change made by ``owner`` (newest first)."""
        restored: list[str] = []
        with self._lock:
            for journal in reversed(self._journal_files()):
                record = json.loads(journal.read_text(encoding="utf-8"))
                if record["owner"] != owner:
                    continue
                for rel, content in record["before"].items():
                    full = self.resolve(rel)
                    if content is None:
                        full.unlink(missing_ok=True)
                    else:
                        _write_text(full, content)
                    restored.append(rel)
                journal.replace(self.journal_dir / "undone" / journal.name)
        return sorted(set(restored))

    # ------------------------------------------------------------------ baseline & promotion

    def seed_from(self, target: Path) -> dict[str, str]:
        """Copy the target codebase into the sandbox; returns its manifest."""
        for rel in list_tree(target):
            dest = self.root / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(target / rel, dest)
        return tree_manifest(self.root)

    def diff(self, baseline: dict[str, str]) -> dict[str, list[str]]:
        current = tree_manifest(self.root)
        return {
            "added": sorted(set(current) - set(baseline)),
            "modified": sorted(p for p in current if p in baseline and current[p] != baseline[p]),
            "deleted": sorted(set(baseline) - set(current)),
        }

    def promote(self, target: Path, baseline: dict[str, str], backup_dir: Path) -> dict[str, list[str]]:
        """Copy the sandbox's changes onto the target, backing up what it replaces."""
        delta = self.diff(baseline)
        backup_dir.mkdir(parents=True, exist_ok=True)
        _write_text(backup_dir / "manifest.json", json.dumps(delta, indent=2))
        for rel in delta["modified"] + delta["deleted"]:
            if (target / rel).is_file():
                (backup_dir / "files" / rel).parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(target / rel, backup_dir / "files" / rel)
        for rel in delta["added"] + delta["modified"]:
            (target / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(self.root / rel, target / rel)
        for rel in delta["deleted"]:
            (target / rel).unlink(missing_ok=True)
        return delta


def restore_promotion(target: Path, backup_dir: Path) -> dict[str, list[str]]:
    """Undo a promotion using its backup. Idempotent."""
    delta = json.loads((backup_dir / "manifest.json").read_text(encoding="utf-8"))
    for rel in delta["added"]:
        (target / rel).unlink(missing_ok=True)
    for rel in delta["modified"] + delta["deleted"]:
        saved = backup_dir / "files" / rel
        if saved.is_file():
            (target / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(saved, target / rel)
    return delta
