"""Thin wrapper over src/services/memory_store.py for the eventual
retrieve_historical_memory and persist_snapshot graph nodes (Phase 8).
Implemented for real now (Phase 7) since the underlying store already
exists — these two functions are what those nodes will call, matching
src/graph/state.py's documented shapes.
"""

from __future__ import annotations

from typing import Optional

from src.models.snapshot import ProjectSnapshot
from src.services.memory_store import MemoryScope, MemoryStore


def retrieve_historical_context(
    store: MemoryStore, scope: MemoryScope, project_ids: list[str], limit: Optional[int] = None
) -> dict[str, list[ProjectSnapshot]]:
    """Matches src/graph/state.py's `historical_context` contract exactly:
    every requested project_id gets a key, `[]` (not a missing key) when
    that project has no history yet — callers must not distinguish
    "not fetched" from "fetched and empty" any other way."""
    return {project_id: store.get_snapshots(scope, project_id, limit=limit) for project_id in project_ids}


def persist_project_snapshot(store: MemoryStore, scope: MemoryScope, snapshot: ProjectSnapshot) -> str:
    return store.add_snapshot(scope, snapshot)
