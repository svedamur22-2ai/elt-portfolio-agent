"""Persistent historical memory (Section 11).

`MemoryScope` carries the three recommended identifiers (organization_id,
portfolio_id, project_id) and maps them onto Mem0's own three scoping
dimensions (`user_id`, `agent_id`, `run_id`). That mapping isn't arbitrary —
Mem0 requires at least one of those three on every `add`/`get_all`/`search`
call, and having exactly three identifiers to map onto exactly three
scoping dimensions means every stored fact is scoped as precisely as
Section 11 asks for, without conflating two identifiers into one string key.

Two implementations:

- `FileMemoryStore`: a small JSON-file-backed store under `data/snapshots/`,
  no external services or API keys. This is what every test and notebook in
  this codebase actually runs against.
- `Mem0MemoryStore`: wraps the real `mem0.Memory()` OSS client. The method
  signatures used below were checked with `inspect.signature()` against the
  installed `mem0ai` package (not taken from documentation alone, since the
  docs' OSS-vs-hosted-platform parameter names weren't fully consistent) —
  confirmed:
      Memory.add(messages, *, user_id=None, agent_id=None, run_id=None,
                 metadata=None, infer=True, ...)
      Memory.get_all(*, filters=None, top_k=20, show_expired=False)
  `Memory()`'s default config uses OpenAI for both fact-extraction and
  embeddings, so it requires `OPENAI_API_KEY` even in "local" OSS mode —
  confirmed by constructing it without one (`OpenAIError: Missing
  credentials...`). That key isn't configured in this environment, so this
  class is implemented for real but not exercised by any test here — same
  status as `JiraCloudRESTSource` in the Jira layer.
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from src.models.snapshot import ProjectSnapshot

DEFAULT_SNAPSHOT_DIR = "data/snapshots"


@dataclass(frozen=True)
class MemoryScope:
    organization_id: str
    portfolio_id: str

    def mem0_filters(self, project_id: Optional[str] = None) -> dict[str, str]:
        filters = {"user_id": self.organization_id, "agent_id": self.portfolio_id}
        if project_id is not None:
            filters["run_id"] = project_id
        return filters


def snapshot_to_fact_text(snapshot: ProjectSnapshot) -> str:
    """Section 11's example format: "Project A: Sprint 23: completion 61%,
    blocked issues: 4, budget consumed: 68%, combined risk: AMBER".

    This text exists ONLY for human/LLM readability if a memory is ever
    surfaced directly in conversation — every actual historical calculation
    (trend_engine.py, Section 12's blocker persistence) deserializes the
    structured `metadata` stored alongside it instead, never re-parses this
    string back into numbers. That separation is what
    `Mem0MemoryStore.add_snapshot`'s `infer=False` protects: Mem0's own LLM
    must never rewrite, round, or drop any of these figures.
    """
    bits = [f"Project {snapshot.project_id}", f"as of {snapshot.snapshot_date.isoformat()}"]
    if snapshot.sprint_id:
        bits.append(f"sprint {snapshot.sprint_id}")
    if snapshot.sprint_completion_pct is not None:
        bits.append(f"completion {snapshot.sprint_completion_pct:.1f}%")
    if snapshot.blocked_issues is not None:
        bits.append(f"blocked issues: {snapshot.blocked_issues}")
    if snapshot.budget_consumption_pct is not None:
        bits.append(f"budget consumed: {snapshot.budget_consumption_pct:.1f}%")
    bits.append(f"combined risk: {snapshot.rag_status.value}")
    return ", ".join(bits)


class MemoryStore(ABC):
    """The only interface callers (memory_agent.py, notebooks, tests)
    depend on — swapping FileMemoryStore for Mem0MemoryStore, or later a
    hosted `MemoryClient`-backed variant, never touches calling code."""

    @abstractmethod
    def add_snapshot(self, scope: MemoryScope, snapshot: ProjectSnapshot) -> str:
        """Returns an opaque memory id."""

    @abstractmethod
    def get_snapshots(
        self, scope: MemoryScope, project_id: str, limit: Optional[int] = None
    ) -> list[ProjectSnapshot]:
        """Chronological, oldest first. `limit` (if given) keeps the most
        recent `limit` snapshots — trend analysis always wants what's most
        recent, never the oldest. Returns `[]` (not an error) when no
        history exists yet for this project — the normal state for a
        project's very first report."""


class FileMemoryStore(MemoryStore):
    def __init__(self, base_dir: str | Path = DEFAULT_SNAPSHOT_DIR) -> None:
        self.base_dir = Path(base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def _path(self, scope: MemoryScope, project_id: str) -> Path:
        safe_name = f"{scope.organization_id}__{scope.portfolio_id}__{project_id}.json"
        return self.base_dir / safe_name

    def add_snapshot(self, scope: MemoryScope, snapshot: ProjectSnapshot) -> str:
        """Upserts by (project_id, snapshot_date) — a second call for the
        same day (a re-run, a scoped chat query landing on the same date as
        the scheduled weekly report) replaces that day's snapshot rather
        than appending a duplicate. Without this, `trend_engine`'s
        multi-sprint blocker counting would double-count same-day reruns as
        if they were separate reporting periods."""
        path = self._path(scope, snapshot.project_id)
        existing: list[dict[str, Any]] = json.loads(path.read_text()) if path.exists() else []
        new_record = json.loads(snapshot.model_dump_json())
        existing = [r for r in existing if r.get("snapshot_date") != new_record["snapshot_date"]]
        existing.append(new_record)
        path.write_text(json.dumps(existing, indent=2))
        return f"{path.stem}#{len(existing) - 1}"

    def get_snapshots(
        self, scope: MemoryScope, project_id: str, limit: Optional[int] = None
    ) -> list[ProjectSnapshot]:
        path = self._path(scope, project_id)
        if not path.exists():
            return []
        raw = json.loads(path.read_text())
        snapshots = [ProjectSnapshot(**record) for record in raw]
        snapshots.sort(key=lambda s: s.snapshot_date)
        return snapshots[-limit:] if limit else snapshots


class Mem0MemoryStore(MemoryStore):
    def __init__(self, memory: Optional[Any] = None) -> None:
        if memory is None:
            from mem0 import Memory  # deferred: pulls in openai/qdrant, unneeded unless this class is used

            memory = Memory()
        self._memory = memory

    def add_snapshot(self, scope: MemoryScope, snapshot: ProjectSnapshot) -> str:
        """Upserts by (project_id, snapshot_date) — same reasoning as
        `FileMemoryStore.add_snapshot`. Mem0's `add()` has no native upsert,
        so this does it explicitly: find any existing memory for this
        project+date via `get_all` and `delete()` it first. Untested in
        this environment (no OPENAI_API_KEY), same status as the rest of
        this class."""
        existing = self._memory.get_all(filters=scope.mem0_filters(snapshot.project_id), top_k=100)
        existing_results = existing.get("results", []) if isinstance(existing, dict) else existing
        target_date = snapshot.snapshot_date.isoformat()
        for item in existing_results:
            metadata = item.get("metadata") or {}
            if metadata.get("snapshot_date") == target_date:
                self._memory.delete(item["id"])

        fact_text = snapshot_to_fact_text(snapshot)
        metadata = json.loads(snapshot.model_dump_json())  # dates/enums -> JSON-safe, matches FileMemoryStore's shape
        result = self._memory.add(
            fact_text,
            user_id=scope.organization_id,
            agent_id=scope.portfolio_id,
            run_id=snapshot.project_id,
            metadata=metadata,
            infer=False,  # verbatim storage — see module docstring
        )
        if isinstance(result, dict) and result.get("results"):
            return result["results"][0].get("id", fact_text)
        return fact_text

    def get_snapshots(
        self, scope: MemoryScope, project_id: str, limit: Optional[int] = None
    ) -> list[ProjectSnapshot]:
        response = self._memory.get_all(filters=scope.mem0_filters(project_id), top_k=limit or 100)
        results = response.get("results", []) if isinstance(response, dict) else response
        snapshots = [ProjectSnapshot(**item["metadata"]) for item in results if item.get("metadata")]
        snapshots.sort(key=lambda s: s.snapshot_date)
        return snapshots[-limit:] if limit else snapshots
