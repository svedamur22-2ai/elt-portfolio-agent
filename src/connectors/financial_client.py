"""Financial datasource abstraction (Section 5).

`FinancialDataSource` is the only interface the rest of the app depends on
— nodes, services, and the Streamlit UI all call this, never a concrete
adapter directly. Today two adapters exist:

- `MockFinancialDataSource`: a small, hand-designed in-memory fixture built
  specifically to demonstrate each risk tier cleanly (GREEN/AMBER/RED/
  forecast-overrun/missing) — useful for tests and for showing the risk
  rules working in isolation, without the real CSV's data-quality quirks
  getting in the way.
- `CSVFinancialDataSource`: reads data/sample/financial_mock_data.csv (real
  monthly data for 5 projects). This is the messier, realistic one — its own
  `remaining_budget` column uses a different formula than Section 5's (see
  `source_reported_remaining_budget` on `FinancialRecord`), which is exactly
  the kind of thing `services/reconciliation.py` exists to catch.

Swapping in SQL Server / Snowflake / Oracle / a REST API later means writing
one more `FinancialDataSource` subclass — `JiraCloudRESTSource` in
jira_client.py is the template for how that seam is meant to work: same
public methods, different `_raw_row_for(...)`-style internals.
"""

from __future__ import annotations

import csv as csv_module
from abc import ABC, abstractmethod
from datetime import datetime
from pathlib import Path
from typing import Optional

import yaml
from pydantic import BaseModel, Field

from src.models.finance import FinancialRecord
from src.utils.time import utcnow

DEFAULT_FIELD_MAPPING_PATH = "config/financial_field_mapping.yaml"
DEFAULT_CSV_PATH = "data/sample/financial_mock_data.csv"

REQUIRED_NUMERIC_FIELDS = ("approved_budget", "actual_spend", "committed_spend", "forecast_spend")


def load_financial_field_mapping(path: str = DEFAULT_FIELD_MAPPING_PATH) -> dict[str, str]:
    with open(path) as f:
        data = yaml.safe_load(f)
    return data["fields"]


class FinancialFetchReport(BaseModel):
    """Mirrors jira_normalizer.NormalizationReport's spirit, scoped to what a
    financial source can actually go wrong in: rows that couldn't be
    normalized into a valid FinancialRecord at all (missing/malformed
    required numeric fields), surfaced rather than silently dropped or
    coerced to 0."""

    rows_skipped: list[str] = Field(default_factory=list)


class FinancialDataSource(ABC):
    """The only interface `FinancialClient`/callers depend on."""

    @abstractmethod
    def get_project_finances(self, project_id: str, reporting_period: str) -> Optional[FinancialRecord]:
        """None (not a zeroed record) when this project/period combination
        has no financial data — reconciliation.detect_missing_financial_record
        turns that into a surfaced finding, never a fabricated zero."""

    @abstractmethod
    def get_portfolio_finances(self, reporting_period: str) -> list[FinancialRecord]:
        """Every project the source has data for, for one period. Does not
        take a project list — a source with no record for a given project in
        this period simply omits it; callers comparing against an expected
        project roster (Section 6's project_mapping.yaml) are the ones who
        turn "expected but absent" into a MISSING finding, since only they
        know what "expected" means."""

    @abstractmethod
    def get_latest_reporting_period(self, project_id: str) -> Optional[str]:
        """None when the source has never seen this project. Used for
        staleness detection — comparing "latest available" against "as of
        now" is how `detect_stale_reporting_period` works, since a period-
        based source has no other notion of "current"."""


def _raw_to_record(
    raw: dict, field_map: dict[str, str], report: FinancialFetchReport, retrieved_timestamp: datetime
) -> Optional[FinancialRecord]:
    def get(semantic: str) -> Optional[str]:
        col = field_map.get(semantic)
        value = raw.get(col) if col else None
        return value.strip() if isinstance(value, str) and value.strip() else None

    project_id = get("project_id")
    reporting_period = get("reporting_period")
    if not project_id or not reporting_period:
        report.rows_skipped.append(f"row missing project_id or reporting_period: {raw!r}")
        return None

    values: dict[str, float] = {}
    for semantic in REQUIRED_NUMERIC_FIELDS:
        raw_value = get(semantic)
        if raw_value is None:
            report.rows_skipped.append(
                f"{project_id}/{reporting_period}: missing required field '{semantic}'"
            )
            return None
        try:
            values[semantic] = float(raw_value)
        except ValueError:
            report.rows_skipped.append(
                f"{project_id}/{reporting_period}: unparseable {semantic}={raw_value!r}"
            )
            return None

    source_remaining_raw = get("source_reported_remaining_budget")
    source_remaining = None
    if source_remaining_raw is not None:
        try:
            source_remaining = float(source_remaining_raw)
        except ValueError:
            pass  # optional field — a bad value here doesn't invalidate the record

    return FinancialRecord(
        project_id=project_id,
        reporting_period=reporting_period,
        approved_budget=values["approved_budget"],
        actual_spend=values["actual_spend"],
        committed_spend=values["committed_spend"],
        forecast_spend=values["forecast_spend"],
        currency=get("currency") or "USD",
        source_reported_remaining_budget=source_remaining,
        retrieved_timestamp=retrieved_timestamp,
    )


class CSVFinancialDataSource(FinancialDataSource):
    """Reads data/sample/financial_mock_data.csv (or any file with the same
    columns, per config/financial_field_mapping.yaml)."""

    def __init__(self, csv_path: str | Path = DEFAULT_CSV_PATH, field_map: Optional[dict[str, str]] = None) -> None:
        self.csv_path = Path(csv_path)
        self.field_map = field_map or load_financial_field_mapping()
        with open(self.csv_path, newline="") as f:
            self._rows: list[dict] = list(csv_module.DictReader(f))
        self.last_fetch_report = FinancialFetchReport()

    def _project_col(self) -> str:
        return self.field_map["project_id"]

    def _period_col(self) -> str:
        return self.field_map["reporting_period"]

    def get_project_finances(self, project_id: str, reporting_period: str) -> Optional[FinancialRecord]:
        report = FinancialFetchReport()
        for row in self._rows:
            if row.get(self._project_col()) == project_id and row.get(self._period_col()) == reporting_period:
                record = _raw_to_record(row, self.field_map, report, utcnow())
                self.last_fetch_report = report
                return record
        self.last_fetch_report = report
        return None

    def get_portfolio_finances(self, reporting_period: str) -> list[FinancialRecord]:
        report = FinancialFetchReport()
        retrieved_timestamp = utcnow()
        records = []
        seen_projects = set()
        for row in self._rows:
            if row.get(self._period_col()) != reporting_period:
                continue
            pid = row.get(self._project_col())
            if pid in seen_projects:
                continue
            record = _raw_to_record(row, self.field_map, report, retrieved_timestamp)
            if record is not None:
                records.append(record)
                seen_projects.add(pid)
        self.last_fetch_report = report
        return records

    def get_latest_reporting_period(self, project_id: str) -> Optional[str]:
        periods = sorted(
            row[self._period_col()] for row in self._rows if row.get(self._project_col()) == project_id
        )
        return periods[-1] if periods else None


# --------------------------------------------------------------------------
# Mock source — small, hand-designed fixture proving each risk tier
# --------------------------------------------------------------------------


def _default_mock_records() -> list[FinancialRecord]:
    """Five illustrative projects, one reporting_period each ("2026-06"),
    each engineered to land cleanly in one detection category. Deliberately
    NOT derived from data/sample/financial_mock_data.csv — that file's real
    numbers are useful precisely because they're messy (see
    CSVFinancialDataSource's docstring), which makes them a poor fixture for
    demonstrating one clean signal at a time.

    MOCK-GREEN:    healthy — spend and forecast both comfortably on track.
    MOCK-AMBER:    consumption over the 75% medium threshold, nothing else wrong.
    MOCK-RED:      consumption over 90%, forecast overrun, negative remaining —
                   every high-tier condition at once.
    MOCK-OVERRUN:  moderate consumption (53%, nowhere near any consumption
                   threshold) but forecast already exceeds approved — proves
                   forecast-based detection fires independently of consumption.
    MOCK-MISSING:  deliberately absent from this list entirely; looking it up
                   demonstrates "missing financial record", not a flag on a
                   record that exists.
    """
    period = "2026-06"
    return [
        FinancialRecord(
            project_id="MOCK-GREEN",
            reporting_period=period,
            approved_budget=200_000.0,
            actual_spend=90_000.0,
            committed_spend=20_000.0,
            forecast_spend=195_000.0,
        ),
        FinancialRecord(
            project_id="MOCK-AMBER",
            reporting_period=period,
            approved_budget=200_000.0,
            actual_spend=160_000.0,
            committed_spend=10_000.0,
            forecast_spend=198_000.0,
        ),
        FinancialRecord(
            project_id="MOCK-RED",
            reporting_period=period,
            approved_budget=200_000.0,
            actual_spend=195_000.0,
            committed_spend=15_000.0,
            forecast_spend=230_000.0,
        ),
        FinancialRecord(
            project_id="MOCK-OVERRUN",
            reporting_period=period,
            approved_budget=150_000.0,
            actual_spend=80_000.0,
            committed_spend=20_000.0,
            forecast_spend=175_000.0,
        ),
        # MOCK-MISSING intentionally has no record.
    ]


class MockFinancialDataSource(FinancialDataSource):
    """In-memory fixture — no file I/O. Ships with `_default_mock_records()`
    unless the caller supplies its own, so tests/notebooks can either use the
    built-in GREEN/AMBER/RED/OVERRUN scenarios or inject a minimal custom
    set for a specific edge case."""

    def __init__(self, records: Optional[list[FinancialRecord]] = None) -> None:
        self._records = records if records is not None else _default_mock_records()

    def get_project_finances(self, project_id: str, reporting_period: str) -> Optional[FinancialRecord]:
        for record in self._records:
            if record.project_id == project_id and record.reporting_period == reporting_period:
                return record
        return None

    def get_portfolio_finances(self, reporting_period: str) -> list[FinancialRecord]:
        return [r for r in self._records if r.reporting_period == reporting_period]

    def get_latest_reporting_period(self, project_id: str) -> Optional[str]:
        periods = sorted(r.reporting_period for r in self._records if r.project_id == project_id)
        return periods[-1] if periods else None


# --------------------------------------------------------------------------
# Forward-compatibility stubs (Section 5: "keep the design ready for...")
# --------------------------------------------------------------------------


class SQLServerFinancialDataSource(FinancialDataSource):
    """Not implemented — no live database/credentials available here.
    Shape once implemented: `get_project_finances` -> parameterized query
    against a `financial_actuals` table filtered by project_id + period;
    `get_latest_reporting_period` -> `SELECT MAX(period) WHERE project_id=?`
    rather than scanning all rows, unlike the CSV source. Connection string
    via env vars (never hard-coded), same pattern as JIRA_URL/JIRA_API_TOKEN
    in .env.example."""

    def __init__(self, connection_string: str) -> None:
        self.connection_string = connection_string

    def get_project_finances(self, project_id: str, reporting_period: str) -> Optional[FinancialRecord]:
        raise NotImplementedError("SQL Server source not implemented — see class docstring")

    def get_portfolio_finances(self, reporting_period: str) -> list[FinancialRecord]:
        raise NotImplementedError("SQL Server source not implemented — see class docstring")

    def get_latest_reporting_period(self, project_id: str) -> Optional[str]:
        raise NotImplementedError("SQL Server source not implemented — see class docstring")


class SnowflakeFinancialDataSource(FinancialDataSource):
    """Not implemented. Shape: same as SQLServerFinancialDataSource but
    against a Snowflake warehouse/schema; `get_latest_reporting_period` is a
    single `MAX(period)` query, cheap even on a large fact table."""

    def __init__(self, account: str, warehouse: str, database: str, schema: str) -> None:
        self.account, self.warehouse, self.database, self.schema = account, warehouse, database, schema

    def get_project_finances(self, project_id: str, reporting_period: str) -> Optional[FinancialRecord]:
        raise NotImplementedError("Snowflake source not implemented — see class docstring")

    def get_portfolio_finances(self, reporting_period: str) -> list[FinancialRecord]:
        raise NotImplementedError("Snowflake source not implemented — see class docstring")

    def get_latest_reporting_period(self, project_id: str) -> Optional[str]:
        raise NotImplementedError("Snowflake source not implemented — see class docstring")


class OracleFinancialDataSource(FinancialDataSource):
    """Not implemented. Shape: same contract, against an Oracle EBS/Fusion
    GL export or direct DB connection."""

    def __init__(self, dsn: str) -> None:
        self.dsn = dsn

    def get_project_finances(self, project_id: str, reporting_period: str) -> Optional[FinancialRecord]:
        raise NotImplementedError("Oracle source not implemented — see class docstring")

    def get_portfolio_finances(self, reporting_period: str) -> list[FinancialRecord]:
        raise NotImplementedError("Oracle source not implemented — see class docstring")

    def get_latest_reporting_period(self, project_id: str) -> Optional[str]:
        raise NotImplementedError("Oracle source not implemented — see class docstring")


class RESTFinancialDataSource(FinancialDataSource):
    """Not implemented. Shape: GET `{base_url}/projects/{project_id}/finances
    ?period={reporting_period}`; auth via bearer token from env, same
    never-hard-coded / never-logged rule as every other connector here."""

    def __init__(self, base_url: str, api_token: str) -> None:
        self.base_url = base_url
        self.api_token = api_token

    def get_project_finances(self, project_id: str, reporting_period: str) -> Optional[FinancialRecord]:
        raise NotImplementedError("REST source not implemented — see class docstring")

    def get_portfolio_finances(self, reporting_period: str) -> list[FinancialRecord]:
        raise NotImplementedError("REST source not implemented — see class docstring")

    def get_latest_reporting_period(self, project_id: str) -> Optional[str]:
        raise NotImplementedError("REST source not implemented — see class docstring")


def build_default_financial_client(csv_path: str = DEFAULT_CSV_PATH) -> CSVFinancialDataSource:
    return CSVFinancialDataSource(csv_path=csv_path)
