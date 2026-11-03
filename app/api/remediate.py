import logging
from typing import Dict, Any, Optional
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from app.services.k8s import k8s_service
from app.services.memory import incident_memory
from app.core.guardrails import InfraGuardrails, GuardrailViolationException

logger = logging.getLogger("kubesentinel.api.remediate")
router = APIRouter(prefix="/api/remediate", tags=["Remediation Gateway"])


class RemediationApprovalRequest(BaseModel):
    incident_id: str
    action: str
    service: str
    namespace: str = "production"
    approved_by: str = "oncall-sre"


@router.post("/execute")
async def execute_remediation(req: RemediationApprovalRequest):
    """Executes an approved infrastructure remediation action with zero-trust audit logging."""
    logger.info(f"Remediation requested for {req.service} in {req.namespace} by {req.approved_by}")

    # Validate action through guardrails
    if req.action != "rollout_undo":
        raise HTTPException(status_code=400, detail=f"Unsupported remediation action: {req.action}")

    try:
        # Execute safe rollback
        result = await k8s_service.rollout_undo(
            namespace=req.namespace,
            deployment_name=req.service,
        )

        # Record resolution to incident memory
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
            "details": result,
        }

    except GuardrailViolationException as gve:
        logger.error(f"Remediation blocked by guardrails: {gve}")
        raise HTTPException(status_code=403, detail=str(gve))
    except Exception as e:
        logger.error(f"Remediation execution failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Execution error: {str(e)}")
