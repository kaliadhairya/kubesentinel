import pytest
from app.services.k8s import k8s_service


@pytest.mark.asyncio
async def test_k8s_get_pods():
    """Validates listing pods in namespace."""
    pods = await k8s_service.get_pods(namespace="production")
    assert len(pods) == 3
    assert "checkout" in pods[0]["name"]


@pytest.mark.asyncio
async def test_k8s_describe_pod():
    """Validates pod details and exit code inspection."""
    desc = await k8s_service.describe_pod(namespace="production", pod_name="checkout-7d4bf6-x9z1")
    assert desc["name"] == "checkout-7d4bf6-x9z1"
    container = desc["container_statuses"][0]
    assert container["last_state"]["terminated"]["exit_code"] == 137
    assert container["last_state"]["terminated"]["reason"] == "OOMKilled"


@pytest.mark.asyncio
async def test_k8s_get_rollout_history():
    """Validates inspection of deployment revisions and image tags."""
    rollouts = await k8s_service.get_rollouts(namespace="production", deployment_name="checkout-service")
    assert rollouts["current_revision"] == 42
    assert "v2.4.2" in rollouts["current_image"]
    assert "v2.4.1" in rollouts["previous_image"]


@pytest.mark.asyncio
async def test_k8s_rollout_undo():
    """Validates executing a rollback in mock cluster."""
    result = await k8s_service.rollout_undo(namespace="production", deployment_name="checkout-service")
    assert result["success"] is True
    assert "checkout:v2.4.1" in result["active_image"]

    # Verify pods healed
    pods = await k8s_service.get_pods(namespace="production")
    assert pods[0]["status"] == "Running"
