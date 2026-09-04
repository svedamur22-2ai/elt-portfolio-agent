"""Assembles the LangGraph StateGraph described in Section 7.

Graph (linear — `classify_request` sets `intent`/`project_filter` but
nothing branches on `intent` yet; conditional routing per intent is a
future refinement, not required for Phase 8):

    START
      -> classify_request
      -> fetch_delivery_data -> validate_delivery_data
      -> fetch_financial_data -> validate_financial_data
      -> unify_projects                      (Phase 5, added here — see state.py)
      -> retrieve_historical_memory
      -> calculate_metrics
      -> analyze_delivery_risk -> analyze_financial_risk -> analyze_cross_domain_risk
      -> validate_findings
      -> generate_response
      -> persist_snapshot
      -> END

Every node above is wrapped with `utils.logging.wrap_node_with_logging`
(Section 23 / Phase 12) — one JSON `graph_node_start`/`graph_node_end`/
`error` line per node per run, uniformly, with zero changes to the node
functions themselves.
"""

from __future__ import annotations

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from src.graph import nodes
from src.graph.nodes import NodeDeps
from src.graph.state import AgentState
from src.utils.logging import wrap_node_with_logging


def build_graph(deps: NodeDeps) -> CompiledStateGraph:
    graph = StateGraph(AgentState)

    graph.add_node("classify_request", wrap_node_with_logging("classify_request", nodes.make_classify_request(deps)))
    graph.add_node(
        "fetch_delivery_data", wrap_node_with_logging("fetch_delivery_data", nodes.make_fetch_delivery_data(deps))
    )
    graph.add_node(
        "validate_delivery_data",
        wrap_node_with_logging("validate_delivery_data", nodes.make_validate_delivery_data(deps)),
    )
    graph.add_node(
        "fetch_financial_data", wrap_node_with_logging("fetch_financial_data", nodes.make_fetch_financial_data(deps))
    )
    graph.add_node(
        "validate_financial_data",
        wrap_node_with_logging("validate_financial_data", nodes.make_validate_financial_data(deps)),
    )
    graph.add_node("unify_projects", wrap_node_with_logging("unify_projects", nodes.make_unify_projects(deps)))
    graph.add_node(
        "retrieve_historical_memory",
        wrap_node_with_logging("retrieve_historical_memory", nodes.make_retrieve_historical_memory(deps)),
    )
    graph.add_node("calculate_metrics", wrap_node_with_logging("calculate_metrics", nodes.make_calculate_metrics(deps)))
    graph.add_node(
        "analyze_delivery_risk", wrap_node_with_logging("analyze_delivery_risk", nodes.make_analyze_delivery_risk(deps))
    )
    graph.add_node(
        "analyze_financial_risk",
        wrap_node_with_logging("analyze_financial_risk", nodes.make_analyze_financial_risk(deps)),
    )
    graph.add_node(
        "analyze_cross_domain_risk",
        wrap_node_with_logging("analyze_cross_domain_risk", nodes.make_analyze_cross_domain_risk(deps)),
    )
    graph.add_node("validate_findings", wrap_node_with_logging("validate_findings", nodes.make_validate_findings(deps)))
    graph.add_node("generate_response", wrap_node_with_logging("generate_response", nodes.make_generate_response(deps)))
    graph.add_node("persist_snapshot", wrap_node_with_logging("persist_snapshot", nodes.make_persist_snapshot(deps)))

    graph.add_edge(START, "classify_request")
    graph.add_edge("classify_request", "fetch_delivery_data")
    graph.add_edge("fetch_delivery_data", "validate_delivery_data")
    graph.add_edge("validate_delivery_data", "fetch_financial_data")
    graph.add_edge("fetch_financial_data", "validate_financial_data")
    graph.add_edge("validate_financial_data", "unify_projects")
    graph.add_edge("unify_projects", "retrieve_historical_memory")
    graph.add_edge("retrieve_historical_memory", "calculate_metrics")
    graph.add_edge("calculate_metrics", "analyze_delivery_risk")
    graph.add_edge("analyze_delivery_risk", "analyze_financial_risk")
    graph.add_edge("analyze_financial_risk", "analyze_cross_domain_risk")
    graph.add_edge("analyze_cross_domain_risk", "validate_findings")
    graph.add_edge("validate_findings", "generate_response")
    graph.add_edge("generate_response", "persist_snapshot")
    graph.add_edge("persist_snapshot", END)

    return graph.compile()
