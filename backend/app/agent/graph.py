"""LangGraph wiring.

START -> prefilter --skip / rule-based triage--> END
                   --redraft--> draft
                   --> triage --no draft needed--> END
                              --needs a draft--> draft -> validate --ok--> END
                                                                   --invalid, 1st time--> draft
                                                                   --invalid twice--> END
"""

from __future__ import annotations

from typing import Any

from langgraph.graph import END, START, StateGraph

from app.agent.llm import StructuredLLM
from app.agent.nodes import (
    make_draft_node,
    make_triage_node,
    prefilter,
    route_after_prefilter,
    route_after_triage,
    route_after_validation,
    validate_draft,
)
from app.agent.state import AgentState


def build_graph(triage_llm: StructuredLLM, draft_llm: StructuredLLM) -> Any:
    g: StateGraph[AgentState] = StateGraph(AgentState)
    g.add_node("prefilter", prefilter)
    g.add_node("triage", make_triage_node(triage_llm))
    g.add_node("draft", make_draft_node(draft_llm))
    g.add_node("validate_draft", validate_draft)

    g.add_edge(START, "prefilter")
    g.add_conditional_edges(
        "prefilter", route_after_prefilter, {"end": END, "draft": "draft", "triage": "triage"}
    )
    g.add_conditional_edges("triage", route_after_triage, {"end": END, "draft": "draft"})
    g.add_edge("draft", "validate_draft")
    g.add_conditional_edges(
        "validate_draft", route_after_validation, {"end": END, "draft": "draft"}
    )
    return g.compile()


async def run_agent(graph: Any, state: AgentState) -> AgentState:
    result: AgentState = await graph.ainvoke(state)
    return result
