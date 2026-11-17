import logging
from typing import Dict, Any, Optional
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from app.services.actuator import actuator_service, GitOpsReconciliationConflictException
from app.services.verifier import spec_verifier
from app.services.memory import incident_memory
from app.core.guardrails import InfraGuardrails, GuardrailViolationException

logger = logging.getLogger("kubesentinel.api.remediate")
router = APIRouter(prefix="/api/remediate", tags=["Remediation Gateway"])


class RemediationApprovalRequest(BaseModel):
    incident_id: str
    action: str = "rollout_undo"
    service: str
    namespace: str = "production"
    target_kind: str = "Deployment"
    mode: str = "CLUSTER_DIRECT"  # "CLUSTER_DIRECT", "GITOPS_PR", "GITOPS_BREAK_GLASS"
    approved_by: str = "oncall-sre"
    break_glass_reason: Optional[str] = None
    fast_forward_verify: bool = True


@router.post("/execute")
async def execute_remediation(req: RemediationApprovalRequest):
    """
    Executes an approved infrastructure remediation action with controller-aware safety routing,
    pre-state snapshotting, and spec-derived health verification.
    """
    logger.info(
        f"Remediation requested for {req.service} in {req.namespace} by {req.approved_by} (mode={req.mode})"
    )

    if req.action != "rollout_undo":
        raise HTTPException(status_code=400, detail=f"Unsupported remediation action: {req.action}")

    try:
        # 1. Execute Actuation Plan (Controller-Aware routing)
        plan_result = await actuator_service.execute_remediation_plan(
            incident_id=req.incident_id,
            action=req.action,
            target_name=req.service,
            namespace=req.namespace,
            target_kind=req.target_kind,
            mode=req.mode,
            approved_by=req.approved_by,
            break_glass_reason=req.break_glass_reason,
        )

        # 2. If mutating cluster, run Spec-Derived Verification
        verification_result = None
        final_state = plan_result["state"]

        if req.mode in ("CLUSTER_DIRECT", "GITOPS_BREAK_GLASS"):
            verify_res = await spec_verifier.verify_remediation(
                incident_id=req.incident_id,
                namespace=req.namespace,
                target_name=req.service,
                fast_forward=req.fast_forward_verify,
            )
            verification_result = verify_res["verification_result"]
            final_state = verify_res["state"]

        # 3. Save resolution memory on success
        if final_state == "RESOLVED":
            await incident_memory.save_incident_resolution(
                incident_id=req.incident_id,
                symptom=f"OOMKilled CrashLoopBackOff on {req.service}",
                resolution=f"Executed rollout undo to previous stable revision by {req.approved_by}.",
            )

        return {
            "success": True,
            "incident_id": req.incident_id,
            "executed_by": req.approved_by,
            "action": req.action,
            "target": req.service,
            "mode": req.mode,
            "state": final_state,
            "plan_result": plan_result,
            "verification_result": verification_result,
        }

    except GitOpsReconciliationConflictException as grce:
        logger.warning(f"Remediation blocked by GitOps conflict: {grce}")
        raise HTTPException(status_code=409, detail=str(grce))
    except GuardrailViolationException as gve:
        logger.error(f"Remediation blocked by guardrails: {gve}")
        raise HTTPException(status_code=403, detail=str(gve))
    except Exception as e:
        logger.error(f"Remediation execution failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Execution error: {str(e)}")
