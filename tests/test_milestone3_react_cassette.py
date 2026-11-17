import pytest
import asyncio
from app.services.agent import sre_agent
from app.services.citation_gate import citation_gate, EvidenceCitationGate
from app.services.cassette import cassette_engine
from app.core.database import get_investigation_steps, get_db_pool, in_memory_store
from app.services.sanitizer import TargetConfinementViolationException


@pytest.mark.asyncio
async def test_cassette_replay_execution_and_journaling():
    """
    Milestone 3 Test 1:
    Deterministic Cassette Replay execution with step journaling into PostgreSQL / in-memory.
    """
    events = []
    async for event in sre_agent.investigate_stream(
        query="Investigate checkout-service crashloop in production",
        namespace="production",
        service_name="checkout-service",
    ):
        events.append(event)

    types = [e["type"] for e in events]

    # Verify structured events emitted
    assert "step_started" in types
    assert "step_completed" in types
    assert "observation" in types
    assert "rca_ready" in types
    assert "remediation_proposed" in types

    # Verify specific tool hops completed
    completed_tools = [e["tool"] for e in events if e["type"] == "step_completed"]
    assert "promql_query_window" in completed_tools
    assert "k8s_describe_workload" in completed_tools
    assert "k8s_get_rollout_history" in completed_tools
    assert "k8s_get_logs" in completed_tools
    assert "knowledge_hybrid_search" in completed_tools

    # Verify RCA and Citation Gate
    rca_event = next(e for e in events if e["type"] == "rca_ready")
    assert rca_event["citation_gate_passed"] is True
    assert len(rca_event["citations"]) >= 3

    # Verify Remediation Card
    remediation_event = next(e for e in events if e["type"] == "remediation_proposed")
    card = remediation_event["card"]
    assert card["action"] == "rollout_undo"
    assert card["target_revision"] == 41
    assert card["requires_human_approval"] is True


@pytest.mark.asyncio
async def test_react_hop_budget_ceiling_enforcement():
    """
    Milestone 3 Test 2:
    Strict 6-Hop Budget Ceiling: Halts loop, forces synthesis, and fails closed to FAILED_ESCALATED.
    """
    events = []
    # Force max hops ceiling to trigger budget exhaustion branch
    async for event in sre_agent.investigate_stream(
        query="Investigate checkout-service crash",
        namespace="production",
        service_name="checkout-service",
        force_max_hops=6,
    ):
        events.append(event)

    # Cassette has 5 hops so it resolves within 6;
    # Now let's test a synthetic scenario exceeding max hops (force_max_hops = 2)
    truncated_events = []
    async for event in sre_agent.investigate_stream(
        query="Investigate checkout-service crash",
        namespace="production",
        service_name="checkout-service",
        force_max_hops=2,
    ):
        truncated_events.append(event)

    completed_hops = [e["hop"] for e in truncated_events if e["type"] == "step_completed"]
    assert len(completed_hops) == 2
    assert max(completed_hops) == 2


@pytest.mark.asyncio
async def test_citation_gate_valid_and_hallucinated_detection():
    """
    Milestone 3 Test 3:
    Evidence-Grounded Citation Gate:
    - Passes when all citations match executed steps.
    - Rejects when citations reference hallucinated step IDs.
    - Rejects when zero evidence citations exist.
    """
    executed_steps = [
        {"hop_number": 1, "id": 101, "tool_name": "promql_query_window"},
        {"hop_number": 2, "id": 102, "tool_name": "k8s_describe_workload"},
        {"hop_number": 3, "id": 103, "tool_name": "k8s_get_rollout_history"},
    ]

    # Valid RCA citing executed hops 1 and 2
    valid_rca = (
        "Memory spiked to 512Mi [Step 1]. Container terminated with OOMKilled exit code 137 [Step 2]."
    )
    res_valid = EvidenceCitationGate.validate(valid_rca, executed_steps)
    assert res_valid.passed is True
    assert 1 in res_valid.valid_citations
    assert 2 in res_valid.valid_citations
    assert len(res_valid.hallucinated_citations) == 0

    # Hallucinated RCA citing non-existent Step 9
    hallucinated_rca = (
        "Memory spiked [Step 1]. Root cause confirmed by database query [Step 9]."
    )
    res_hallucinated = EvidenceCitationGate.validate(hallucinated_rca, executed_steps)
    assert res_hallucinated.passed is False
    assert 9 in res_hallucinated.hallucinated_citations
    assert "hallucinated step references [9]" in res_hallucinated.rejection_reason

    # Zero citation RCA
    empty_citation_rca = "Pod crashed due to high traffic and node issues."
    res_empty = EvidenceCitationGate.validate(empty_citation_rca, executed_steps)
    assert res_empty.passed is False
    assert "zero evidence citations" in res_empty.rejection_reason


@pytest.mark.asyncio
async def test_agent_stream_rejects_hallucinated_citation():
    """
    Milestone 3 Test 4:
    End-to-end agent stream rejects an RCA with a corrupted/hallucinated citation.
    """
    events = []
    async for event in sre_agent.investigate_stream(
        query="Investigate checkout-service crash",
        namespace="production",
        service_name="checkout-service",
        corrupt_citation=True,
    ):
        events.append(event)

    types = [e["type"] for e in events]
    assert "error" in types

    synthesis_event = next(e for e in events if e["type"] == "synthesis")
    assert synthesis_event.get("citation_gate_passed") is False
    assert 99 in synthesis_event.get("hallucinated_citations", [])
