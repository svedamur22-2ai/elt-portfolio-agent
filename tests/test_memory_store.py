from datetime import date

import pytest

from src.models.common import RAGStatus
from src.models.snapshot import ProjectSnapshot
from src.services.memory_store import FileMemoryStore, MemoryScope, snapshot_to_fact_text

SCOPE = MemoryScope(organization_id="org-test", portfolio_id="portfolio-test")


def _snapshot(project_id="P1", snapshot_date=date(2026, 1, 1), completion=50.0, blocked=2, rag=RAGStatus.AMBER, major_blockers=None) -> ProjectSnapshot:
    return ProjectSnapshot(
        snapshot_date=snapshot_date,
        project_id=project_id,
        sprint_completion_pct=completion,
        blocked_issues=blocked,
        rag_status=rag,
        major_blockers=major_blockers or [],
    )


class TestMemoryScope:
    def test_mem0_filters_without_project_id(self):
        assert SCOPE.mem0_filters() == {"user_id": "org-test", "agent_id": "portfolio-test"}

    def test_mem0_filters_with_project_id(self):
        assert SCOPE.mem0_filters("P1") == {"user_id": "org-test", "agent_id": "portfolio-test", "run_id": "P1"}


class TestSnapshotToFactText:
    def test_includes_all_present_fields(self):
        text = snapshot_to_fact_text(_snapshot(completion=61.0, blocked=4, rag=RAGStatus.RED))
        assert "completion 61.0%" in text
        assert "blocked issues: 4" in text
        assert "combined risk: RED" in text

    def test_omits_fields_that_are_none(self):
        s = ProjectSnapshot(snapshot_date=date(2026, 1, 1), project_id="P1", rag_status=RAGStatus.UNKNOWN)
        text = snapshot_to_fact_text(s)
        assert "completion" not in text
        assert "blocked issues" not in text
        assert "combined risk: UNKNOWN" in text


class TestFileMemoryStore:
    def test_round_trip_add_then_get(self, tmp_path):
        store = FileMemoryStore(base_dir=tmp_path)
        snap = _snapshot()
        store.add_snapshot(SCOPE, snap)

        retrieved = store.get_snapshots(SCOPE, "P1")
        assert len(retrieved) == 1
        assert retrieved[0].project_id == "P1"
        assert retrieved[0].sprint_completion_pct == 50.0

    def test_empty_before_anything_is_stored(self, tmp_path):
        store = FileMemoryStore(base_dir=tmp_path)
        assert store.get_snapshots(SCOPE, "NEVER-SEEN") == []

    def test_multiple_snapshots_returned_chronologically(self, tmp_path):
        store = FileMemoryStore(base_dir=tmp_path)
        # Deliberately added out of order
        store.add_snapshot(SCOPE, _snapshot(snapshot_date=date(2026, 2, 1), completion=80.0))
        store.add_snapshot(SCOPE, _snapshot(snapshot_date=date(2026, 1, 1), completion=50.0))

        retrieved = store.get_snapshots(SCOPE, "P1")
        assert [s.snapshot_date for s in retrieved] == [date(2026, 1, 1), date(2026, 2, 1)]

    def test_limit_keeps_the_most_recent_not_the_oldest(self, tmp_path):
        store = FileMemoryStore(base_dir=tmp_path)
        for i, d in enumerate([date(2026, 1, 1), date(2026, 1, 8), date(2026, 1, 15)]):
            store.add_snapshot(SCOPE, _snapshot(snapshot_date=d, completion=float(i)))

        retrieved = store.get_snapshots(SCOPE, "P1", limit=2)
        assert [s.snapshot_date for s in retrieved] == [date(2026, 1, 8), date(2026, 1, 15)]

    def test_different_projects_are_isolated(self, tmp_path):
        store = FileMemoryStore(base_dir=tmp_path)
        store.add_snapshot(SCOPE, _snapshot(project_id="P1"))
        store.add_snapshot(SCOPE, _snapshot(project_id="P2"))

        assert len(store.get_snapshots(SCOPE, "P1")) == 1
        assert len(store.get_snapshots(SCOPE, "P2")) == 1

    def test_second_write_same_day_upserts_rather_than_duplicates(self, tmp_path):
        """Regression: caught while building Phase 9's notebook — a scoped
        chat query and the scheduled weekly report can both persist a
        snapshot for the same project on the same day. Without upsert
        semantics, that silently doubled `sprint_count_blocked` for
        whichever project happened to get queried twice in one day."""
        store = FileMemoryStore(base_dir=tmp_path)
        store.add_snapshot(SCOPE, _snapshot(snapshot_date=date(2026, 1, 1), completion=50.0))
        store.add_snapshot(SCOPE, _snapshot(snapshot_date=date(2026, 1, 1), completion=80.0))  # same day, different run

        retrieved = store.get_snapshots(SCOPE, "P1")
        assert len(retrieved) == 1
        assert retrieved[0].sprint_completion_pct == 80.0  # the later write wins

    def test_upsert_does_not_disturb_other_days(self, tmp_path):
        store = FileMemoryStore(base_dir=tmp_path)
        store.add_snapshot(SCOPE, _snapshot(snapshot_date=date(2026, 1, 1)))
        store.add_snapshot(SCOPE, _snapshot(snapshot_date=date(2026, 1, 8)))
        store.add_snapshot(SCOPE, _snapshot(snapshot_date=date(2026, 1, 1), completion=99.0))

        retrieved = store.get_snapshots(SCOPE, "P1")
        assert len(retrieved) == 2
        assert next(s for s in retrieved if s.snapshot_date == date(2026, 1, 1)).sprint_completion_pct == 99.0

    def test_different_scopes_are_isolated(self, tmp_path):
        store = FileMemoryStore(base_dir=tmp_path)
        other_scope = MemoryScope(organization_id="org-other", portfolio_id="portfolio-test")
        store.add_snapshot(SCOPE, _snapshot(project_id="P1"))

        assert len(store.get_snapshots(SCOPE, "P1")) == 1
        assert store.get_snapshots(other_scope, "P1") == []

    def test_major_blockers_round_trip_exactly(self, tmp_path):
        store = FileMemoryStore(base_dir=tmp_path)
        store.add_snapshot(SCOPE, _snapshot(major_blockers=["A-1", "A-2"]))
        retrieved = store.get_snapshots(SCOPE, "P1")
        assert retrieved[0].major_blockers == ["A-1", "A-2"]

    def test_add_snapshot_returns_a_usable_id(self, tmp_path):
        store = FileMemoryStore(base_dir=tmp_path)
        memory_id = store.add_snapshot(SCOPE, _snapshot())
        assert isinstance(memory_id, str) and memory_id


class TestMem0MemoryStoreConstruction:
    """Real behavior against the installed mem0ai package — not mocked.
    Confirms the documented requirement (Mem0's default embedder/LLM is
    OpenAI, even in OSS "local" mode) actually holds, rather than trusting
    the docs alone."""

    def test_requires_openai_api_key(self, monkeypatch):
        pytest.importorskip("mem0")
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        from src.services.memory_store import Mem0MemoryStore

        with pytest.raises(Exception):
            Mem0MemoryStore()

    def test_accepts_a_pre_constructed_client_without_needing_a_key(self):
        """Dependency injection seam: a caller who already has a working
        mem0.Memory() (or a test double) can hand it in directly."""
        pytest.importorskip("mem0")
        from src.services.memory_store import Mem0MemoryStore

        class FakeMem0Client:
            def __init__(self):
                self.added = []

            def add(self, messages, **kwargs):
                self.added.append((messages, kwargs))
                return {"results": [{"id": "fake-id-1"}]}

            def get_all(self, **kwargs):
                return {"results": []}

        fake = FakeMem0Client()
        store = Mem0MemoryStore(memory=fake)
        memory_id = store.add_snapshot(SCOPE, _snapshot())
        assert memory_id == "fake-id-1"
        assert fake.added[0][1]["infer"] is False  # verbatim storage, never LLM-paraphrased
        assert fake.added[0][1]["user_id"] == "org-test"
        assert fake.added[0][1]["agent_id"] == "portfolio-test"
        assert fake.added[0][1]["run_id"] == "P1"

    def test_get_snapshots_reconstructs_from_metadata_via_fake_client(self):
        pytest.importorskip("mem0")
        from src.services.memory_store import Mem0MemoryStore
        import json

        snap = _snapshot()

        class FakeMem0Client:
            def get_all(self, **kwargs):
                return {"results": [{"id": "x", "metadata": json.loads(snap.model_dump_json())}]}

        store = Mem0MemoryStore(memory=FakeMem0Client())
        retrieved = store.get_snapshots(SCOPE, "P1")
        assert len(retrieved) == 1
        assert retrieved[0].project_id == "P1"
        assert retrieved[0].sprint_completion_pct == 50.0

    def test_add_snapshot_deletes_any_existing_same_day_memory_first(self):
        """Mirrors FileMemoryStore's upsert-by-day regression test — Mem0
        has no native upsert, so add_snapshot must find and delete the
        old memory for this project+date itself before adding the new
        one."""
        pytest.importorskip("mem0")
        import json
        from src.services.memory_store import Mem0MemoryStore

        old_snap = _snapshot(snapshot_date=date(2026, 1, 1), completion=50.0)

        class FakeMem0Client:
            def __init__(self):
                self.deleted = []
                self.added = []

            def get_all(self, **kwargs):
                return {"results": [{"id": "old-memory-id", "metadata": json.loads(old_snap.model_dump_json())}]}

            def delete(self, memory_id):
                self.deleted.append(memory_id)

            def add(self, messages, **kwargs):
                self.added.append((messages, kwargs))
                return {"results": [{"id": "new-memory-id"}]}

        fake = FakeMem0Client()
        store = Mem0MemoryStore(memory=fake)
        new_snap = _snapshot(snapshot_date=date(2026, 1, 1), completion=90.0)  # same day as old_snap
        memory_id = store.add_snapshot(SCOPE, new_snap)

        assert fake.deleted == ["old-memory-id"]
        assert memory_id == "new-memory-id"

    def test_add_snapshot_does_not_delete_a_different_days_memory(self):
        pytest.importorskip("mem0")
        import json
        from src.services.memory_store import Mem0MemoryStore

        other_day_snap = _snapshot(snapshot_date=date(2026, 1, 8))

        class FakeMem0Client:
            def __init__(self):
                self.deleted = []

            def get_all(self, **kwargs):
                return {"results": [{"id": "other-day-id", "metadata": json.loads(other_day_snap.model_dump_json())}]}

            def delete(self, memory_id):
                self.deleted.append(memory_id)

            def add(self, messages, **kwargs):
                return {"results": [{"id": "new-id"}]}

        fake = FakeMem0Client()
        store = Mem0MemoryStore(memory=fake)
        store.add_snapshot(SCOPE, _snapshot(snapshot_date=date(2026, 1, 1)))

        assert fake.deleted == []
