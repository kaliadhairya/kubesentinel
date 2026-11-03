import pytest
from app.services.agent import sre_agent


@pytest.mark.asyncio
async def test_sre_agent_investigate_stream():
    """Validates end-to-end execution of the autonomous SRE ReAct loop."""
    events = []
    async for event in sre_agent.investigate_stream(
        query="checkout-service pods are crashing with exit code 137 in production. Investigate.",
        namespace="production",
        service_name="checkout-service",
    ):
        events.append(event)

    event_types = [e["type"] for e in events]

    # Ensure all key ReAct phases executed
    assert "thought" in event_types
    assert "tool_call" in event_types
    assert "observation" in event_types
    assert "chart" in event_types
    assert "synthesis" in event_types
    assert "remediation_card" in event_types

    # Validate synthesis content
    synthesis = next(e for e in events if e["type"] == "synthesis")
    assert "Root Cause Analysis" in synthesis["content"]
    assert "checkout-service" in synthesis["content"]

    # Validate remediation card
    card_event = next(e for e in events if e["type"] == "remediation_card")
    card = card_event["card"]
    assert card["action"] == "rollout_undo"
    assert "kubectl rollout undo" in card["command"]
    assert card["safety_status"] == "PASSED_GUARDRAILS"
