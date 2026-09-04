"""Structured observability logging (Section 23).

Implemented Phase 8 alongside the graph, since request_id/graph-nodes-
executed/latency are graph-level concerns. Contract fixed now: every log
line is one JSON object with at least `request_id` and `timestamp`; a
dedicated `credential`-shaped field must never appear (see .env.example /
Section 22 — enforced by code review, not by a runtime filter, since a
filter can't know every future field name that might carry a secret).

Phase 12: actually implemented. `log_event` is the one place a JSON line
reaches stdout; `wrap_node_with_logging` applies it uniformly to every
LangGraph node in `workflow.build_graph`, so `graph_node_start` /
`graph_node_end` / `error` / `latency_ms` require zero per-node code. A few
nodes (fetch_delivery_data, fetch_financial_data, retrieve_historical_memory,
persist_snapshot) additionally call `log_event` directly for
`records_retrieved` / `memory_retrieved` / `datasource_access`, since "how
many records came back from which source" isn't visible from the node
wrapper's black-box view of a plain dict return value.

Redaction is defense-in-depth, not the primary control: no node in this
codebase ever puts a real secret into state or a log field in the first
place (constructors read credentials once, at startup, from env vars — see
.env.example). But a field is redacted by NAME here rather than trusted to
stay that way forever, since a future field could be added without anyone
re-reading this file's docstring.
"""

from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timezone
from typing import Any, Callable

REQUIRED_FIELDS = (
    "request_id",
    "timestamp",
    "event",
)

# event examples this module emits:
#   "graph_node_start", "graph_node_end", "error",
#   "records_retrieved", "memory_retrieved", "datasource_access"

_SENSITIVE_KEY_MARKERS = ("token", "key", "secret", "password", "credential")
_REDACTED = "***REDACTED***"


def _is_sensitive_key(key: str) -> bool:
    lowered = key.lower()
    return any(marker in lowered for marker in _SENSITIVE_KEY_MARKERS)


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: (_REDACTED if isinstance(k, str) and _is_sensitive_key(k) else _redact(v)) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact(v) for v in value]
    return value


def log_event(event: str, request_id: str, **fields: Any) -> None:
    """Emits one JSON line to stdout: `{request_id, timestamp, event, ...fields}`.

    Any field whose name looks credential-shaped (see `_SENSITIVE_KEY_MARKERS`)
    is replaced with a redaction marker rather than logged, at any nesting
    depth — the required contract from Section 22 is enforced here, not left
    to callers to remember per call site.
    """
    payload: dict[str, Any] = {
        "request_id": request_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "event": event,
    }
    for key, value in fields.items():
        payload[key] = _REDACTED if _is_sensitive_key(key) else _redact(value)
    print(json.dumps(payload, default=str), file=sys.stdout)


def wrap_node_with_logging(node_name: str, fn: Callable[[Any], dict]) -> Callable[[Any], dict]:
    """Wraps one graph node so every node emits `graph_node_start` /
    `graph_node_end` (with `latency_ms`) / `error` uniformly, with zero
    changes to the node functions themselves — applied once, in
    `workflow.build_graph`, to every node factory's output.

    Re-raises on failure after logging `error`: a node crashing here means a
    genuine bug (every known, recoverable source failure is already caught
    and degraded to UNKNOWN *inside* the node — see project_unifier.py and
    fetch_financial_data's docstrings), so this must not swallow it.
    """

    def wrapped(state: Any) -> dict:
        request_id = state.get("request_id", "UNKNOWN")
        log_event("graph_node_start", request_id, node=node_name)
        start = time.monotonic()
        try:
            result = fn(state)
        except Exception as exc:
            latency_ms = (time.monotonic() - start) * 1000
            log_event(
                "error",
                request_id,
                node=node_name,
                latency_ms=round(latency_ms, 2),
                error_type=type(exc).__name__,
                error_message=str(exc),
            )
            raise
        latency_ms = (time.monotonic() - start) * 1000
        log_event("graph_node_end", request_id, node=node_name, latency_ms=round(latency_ms, 2))
        return result

    return wrapped
