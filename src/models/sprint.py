from datetime import date
from typing import Optional

from pydantic import BaseModel, field_validator


class Sprint(BaseModel):
    sprint_id: str
    sprint_name: str
    project_id: str
    start_date: date
    end_date: date
    sprint_status: str
    """TODO / ACTIVE / CLOSED — normalized from source, not the RAG status."""

    committed_story_points: float = 0.0
    completed_story_points: float = 0.0
    completion_pct: Optional[float] = None
    """Calculated by services/sprint_metrics.py — never set directly from source."""
    carryover_story_points: float = 0.0

    @field_validator("committed_story_points", "completed_story_points", "carryover_story_points")
    @classmethod
    def _non_negative(cls, v: float) -> float:
        if v < 0:
            raise ValueError("story points must be >= 0")
        return v
