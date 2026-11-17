import pytest
from app.services.actuator import actuator_service, GitOpsReconciliationConflictException
from app.services.verifier import spec_verifier
from app.services.k8s import k8s_service
from app.core.database import record_incident_signal, in_memory_store, get_remediation_action


@pytest.fixture(autouse=True)
def reset_k8s_state():
    k8s_service.reset_mock_state()
    yield
    k8s_service.reset_mock_state()


@pytest.mark.asyncio
async def test_gitops_controller_detection_and_conflict_block():
    """
    Milestone 4 Test 1:
    Controller-Aware Actuator: Direct mutation on ArgoCD workload is blocked to prevent self-heal race.
    """
    # 1. Setup mock incident
    res = await record_incident_signal(
        cluster_id="default-cluster",
        namespace="production",
        target_kind="Deployment",
        target_name="checkout-service",
        severity="CRITICAL",
        source="MANUAL_CLI",
        signal_name="CrashDetected",
        raw_payload={},
    )
    inc_id = res["incident_id"]

    # 2. Configure workload as ArgoCD-managed
    actuator_service.set_mock_workload_annotations("checkout-service", {
        "argocd.argoproj.io/tracking-id": "checkout-app:apps/Deployment:production/checkout-service",
        "argocd.argoproj.io/instance": "checkout-production",
        "app.kubernetes.io/managed-by": "argocd",
    })

    # Direct cluster mutation should be blocked
    with pytest.raises(GitOpsReconciliationConflictException) as exc_info:
        await actuator_service.execute_remediation_plan(
            incident_id=inc_id,
            action="rollout_undo",
            target_name="checkout-service",
            namespace="production",
            mode="CLUSTER_DIRECT",
        )
    assert "Direct cluster mutation blocked" in str(exc_info.value)
    assert "ARGOCD" in str(exc_info.value)


@pytest.mark.asyncio
async def test_gitops_pr_mode_generation():
    """
    Milestone 4 Test 2:
    GITOPS_PR mode generates a unified YAML diff and GitHub PR without direct cluster mutation.
    """
    res = await record_incident_signal(
        cluster_id="default-cluster",
        namespace="production",
        target_kind="Deployment",
        target_name="checkout-service",
        severity="CRITICAL",
        source="MANUAL_CLI",
        signal_name="CrashDetected",
        raw_payload={},
    )
    inc_id = res["incident_id"]

    pr_result = await actuator_service.execute_remediation_plan(
        incident_id=inc_id,
        action="rollout_undo",
        target_name="checkout-service",
        namespace="production",
        mode="GITOPS_PR",
    )

    assert pr_result["success"] is True
    assert pr_result["mode"] == "GITOPS_PR"
    assert pr_result["gitops_pr_url"] is not None
    diff_spec = pr_result["remediation_result"]["diff_spec"]
    assert "--- a/deploy/environments/production/checkout-service.yaml" in diff_spec["yaml_diff"]
    assert "+        image: checkout:v2.4.1" in diff_spec["yaml_diff"]


@pytest.mark.asyncio
async def test_gitops_break_glass_mutation():
    """
    Milestone 4 Test 3:
    GITOPS_BREAK_GLASS bypasses ArgoCD lock, logs rationale, and mutates cluster directly.
    """
    res = await record_incident_signal(
        cluster_id="default-cluster",
        namespace="production",
        target_kind="Deployment",
        target_name="checkout-service",
        severity="CRITICAL",
        source="MANUAL_CLI",
        signal_name="CrashDetected",
        raw_payload={},
    )
    inc_id = res["incident_id"]

    bg_result = await actuator_service.execute_remediation_plan(
        incident_id=inc_id,
        action="rollout_undo",
        target_name="checkout-service",
        namespace="production",
        mode="GITOPS_BREAK_GLASS",
        break_glass_reason="Checkout revenue impacted during Black Friday outage",
    )

    assert bg_result["success"] is True
    assert bg_result["mode"] == "GITOPS_BREAK_GLASS"
    assert bg_result["state"] == "VERIFYING"
    assert bg_result["pre_snapshot"]["current_image"] == "checkout:v2.4.2"


@pytest.mark.asyncio
async def test_spec_derived_verifier_soak_calculation_and_success():
    """
    Milestone 4 Test 4:
    Spec-Derived Verifier calculates dynamic soak window and transitions incident to RESOLVED.
    """
    # 1. Validate mathematical calculation: startup(30) + delay(30) + 3*period(10) + 60 = 150s
    soak_window = spec_verifier.calculate_soak_window({
        "containers": [
            {
                "readinessProbe": {"initialDelaySeconds": 30, "periodSeconds": 10},
                "startupProbe": {"initialDelaySeconds": 30},
            }
        ]
    })
    assert soak_window == 150

    # 2. Setup verified incident
    res = await record_incident_signal(
        cluster_id="default-cluster",
        namespace="production",
        target_kind="Deployment",
        target_name="checkout-service",
        severity="CRITICAL",
        source="MANUAL_CLI",
        signal_name="CrashDetected",
        raw_payload={},
    )
    inc_id = res["incident_id"]

    # Execute rollback in non-gitops mode
    actuator_service.set_mock_workload_annotations("checkout-service", {})
    await actuator_service.execute_remediation_plan(
        incident_id=inc_id,
        action="rollout_undo",
        target_name="checkout-service",
        namespace="production",
        mode="CLUSTER_DIRECT",
    )

    # Run verification soak
    verify_res = await spec_verifier.verify_remediation(
        incident_id=inc_id,
        namespace="production",
        target_name="checkout-service",
        fast_forward=True,
    )
    assert verify_res["success"] is True
    assert verify_res["state"] == "RESOLVED"
    assert verify_res["verification_result"]["status"] == "HEALTHY"


@pytest.mark.asyncio
async def test_spec_derived_verifier_regression_auto_revert():
    """
    Milestone 4 Test 5:
    When regression/crash occurs during soak, auto-revert triggers and incident escalates to FAILED_ESCALATED.
    """
    res = await record_incident_signal(
        cluster_id="default-cluster",
        namespace="production",
        target_kind="Deployment",
        target_name="checkout-service",
        severity="CRITICAL",
        source="MANUAL_CLI",
        signal_name="CrashDetected",
        raw_payload={},
    )
    inc_id = res["incident_id"]

    # Execute rollback
    actuator_service.set_mock_workload_annotations("checkout-service", {})
    await actuator_service.execute_remediation_plan(
        incident_id=inc_id,
        action="rollout_undo",
        target_name="checkout-service",
        namespace="production",
        mode="CLUSTER_DIRECT",
    )

    # Run verification simulating regression
    verify_res = await spec_verifier.verify_remediation(
        incident_id=inc_id,
        namespace="production",
        target_name="checkout-service",
        fast_forward=True,
        simulate_regression=True,
    )

    assert verify_res["success"] is False
    assert verify_res["state"] == "FAILED_ESCALATED"
    vres = verify_res["verification_result"]
    assert vres["status"] == "REGRESSION_DETECTED"
    assert vres["auto_revert_executed"] is True
