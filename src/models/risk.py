from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field

from src.utils.time import utcnow

from .common import RiskSeverity


class Risk(BaseModel):
    risk_id: str
    project_id: str
    category: str
    """e.g. "Delivery", "Financial", "Data Quality" — free-form but should
    align to a reason_code family in config/risk_rules.yaml."""
    severity: RiskSeverity
    description: str
    evidence: list[str] = Field(default_factory=list)
    """Every RED/HIGH risk must have >=1 evidence line (Accuracy Check 7).
    Populated with concrete numbers/issue keys, never prose like "seems
    risky" — services/risk_engine.py enforces this at construction time in
    Phase 6."""
    recommended_action: Optional[str] = None
    reason_codes: list[str] = Field(default_factory=list)
    detected_at: datetime = Field(default_factory=utcnow)
