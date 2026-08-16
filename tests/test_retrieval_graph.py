"""Unit tests for LangGraph retrieval routing and compile."""

from __future__ import annotations

from app.models import QueryMode, RagMode
from app.retrieval.graph import (
    build_retrieval_graph,
    reset_retrieval_graph,
    route_after_citations,
    route_after_generate,
    route_after_grade,
    route_after_retrieve,
)


def test_route_after_retrieve_regular_goes_expand() -> None:
    """Regular rag_mode skips grading."""
    assert route_after_retrieve({"rag_mode": RagMode.REGULAR}) == "expand"


def test_route_after_retrieve_agentic_goes_grade() -> None:
    """Agentic rag_mode grades before expand."""
    assert route_after_retrieve({"rag_mode": RagMode.AGENTIC}) == "grade"


def test_route_after_grade_relevant_expands() -> None:
    """YES grade exits the rewrite loop."""
    assert (
        route_after_grade(
            {"relevant": True, "chunks": [{"id": 1}], "attempt": 1, "max_iters": 3}
        )
        == "expand"
    )


def test_route_after_grade_rewrites_when_budget_left() -> None:
    """Not relevant with attempts remaining rewrites."""
    assert (
        route_after_grade(
            {"relevant": False, "chunks": [{"id": 1}], "attempt": 1, "max_iters": 3}
        )
        == "rewrite"
    )


def test_route_after_grade_exhausts_to_expand() -> None:
    """Last failed grade still expands with last chunks."""
    assert (
        route_after_grade(
            {"relevant": False, "chunks": [{"id": 1}], "attempt": 3, "max_iters": 3}
        )
        == "expand"
    )


def test_route_after_grade_empty_expands() -> None:
    """Empty retrieval does not spin rewrite."""
    assert (
        route_after_grade(
            {"relevant": False, "chunks": [], "attempt": 1, "max_iters": 3}
        )
        == "expand"
    )


def test_route_after_citations_no_docs() -> None:
    """No chunks skips generation."""
    assert route_after_citations({"chunks": []}) == "no_docs"
    assert route_after_citations({"chunks": [{"id": "a"}]}) == "generate"


def test_route_after_generate_primary_retry_then_error() -> None:
    """One same-model retry then error node (sample-style fallback)."""
    assert route_after_generate({"generation_ok": True, "generate_attempt": 1}) == "done"
    assert (
        route_after_generate({"generation_ok": False, "generate_attempt": 1}) == "retry"
    )
    assert (
        route_after_generate({"generation_ok": False, "generate_attempt": 2}) == "error"
    )


def test_build_retrieval_graph_compiles() -> None:
    """StateGraph compiles with hybrid + agentic nodes."""
    reset_retrieval_graph()
    graph = build_retrieval_graph()
    assert graph is not None
    # Touch mode enum so regressions notice missing QueryMode wiring.
    assert QueryMode.LOCAL.value == "local"
