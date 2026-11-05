import logging
from typing import Dict, Any, List, Optional
from fastapi import APIRouter, BackgroundTasks
from pydantic import BaseModel
from app.services.agent import sre_agent

logger = logging.getLogger("kubesentinel.api.alertmanager")
router = APIRouter(prefix="/api/v1/webhook", tags=["Alertmanager Webhook"])


class AlertItem(BaseModel):
    status: str
    labels: Dict[str, str] = {}
    annotations: Dict[str, str] = {}
    startsAt: Optional[str] = ""


class AlertmanagerWebhookPayload(BaseModel):
    version: str = "4"
    groupKey: str = ""
    status: str
    receiver: str = ""
    alerts: List[Dict[str, Any]] = []


@router.post("/alertmanager")
async def receive_alert(payload: Dict[str, Any], background_tasks: BackgroundTasks):
    """Webhook ingress for Prometheus Alertmanager notifications."""
    alerts = payload.get("alerts", [])
    logger.info(f"Received Alertmanager webhook with {len(alerts)} alerts.")

    processed = []
    for alert in alerts:
        labels = alert.get("labels", {})
        annotations = alert.get("annotations", {})
        alert_name = labels.get("alertname", "UnknownAlert")
        service = labels.get("service") or labels.get("app", "unknown-service")
        namespace = labels.get("namespace", "production")
        severity = labels.get("severity", "critical")

        incident_id = f"INC-{service[:8].upper()}-{alert_name[:6].upper()}"

        processed.append({
            "incident_id": incident_id,
            "alert_name": alert_name,
            "service": service,
            "namespace": namespace,
            "severity": severity,
            "summary": annotations.get("summary") or annotations.get("description", "Alert triggered"),
        })

    return {
        "status": "accepted",
        "processed_count": len(processed),
        "incidents": processed,
    }
