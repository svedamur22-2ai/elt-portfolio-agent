"""Shared enums and provenance primitives used across every model.

Design note (Accuracy Check 3 — Data Completeness): model fields stay
properly typed (`Optional[float] = None`, etc.) so Python/Pydantic can still
do arithmetic and validation on them. The literal display sentinels the spec
asks for ("UNKNOWN", "NOT AVAILABLE", "NOT MAPPED") are a presentation-layer
concern — `services/reconciliation.py` (Phase 6) renders `None` to the
correct sentinel depending on *why* the value is missing. Baking string
sentinels into numeric fields would make every downstream calculation have to
defensively check for stringly-typed magic values, which is exactly the kind
of silent-fill risk Section 13 is trying to prevent.
"""

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field

from src.utils.time import utcnow


class DisplaySentinel(str, Enum):
    """Presentation-layer-only sentinels. Never assigned to a typed field."""

    UNKNOWN = "UNKNOWN"
    NOT_AVAILABLE = "NOT AVAILABLE"
    NOT_MAPPED = "NOT MAPPED"


class RAGStatus(str, Enum):
    GREEN = "GREEN"
    AMBER = "AMBER"
    RED = "RED"
    UNKNOWN = "UNKNOWN"


class RiskSeverity(str, Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


class ConfidenceLevel(str, Enum):
    HIGH = "HIGH CONFIDENCE"
    MEDIUM = "MEDIUM CONFIDENCE"
    LOW = "LOW CONFIDENCE"


class TrendDirection(str, Enum):
    IMPROVING = "IMPROVING"
    STABLE = "STABLE"
    DETERIORATING = "DETERIORATING"
    BASELINE = "BASELINE"  # no prior snapshot exists yet — never inferred


class SourceSystem(str, Enum):
    JIRA = "Jira"
    FINANCE = "Finance"
    MEMORY = "Historical Snapshot"
    CALCULATED = "Calculated"


class SourceProvenance(BaseModel):
    """Accuracy Check 1: every material fact carries this alongside it.

    `retrieved_timestamp` is when *this pipeline run* pulled the record, not
    when the source system last changed it — freshness (Accuracy Check 2) is
    computed from this against the configured threshold in risk_rules.yaml.
    """

    source_system: SourceSystem
    source_record_id: str
    retrieved_timestamp: datetime = Field(default_factory=utcnow)
