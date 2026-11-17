import logging
from typing import Dict, Any, List, Optional
from fastapi import APIRouter, Header, HTTPException, Request, Response, status
from pydantic import BaseModel
from app.core.config import settings
from app.core.database import record_incident_signal
from app.services.ingress import verify_alertmanager_hmac

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
async def receive_alert(
    request: Request,
    x_signature: Optional[str] = Header(None, alias="X-Signature"),
    x_alertmanager_signature: Optional[str] = Header(None, alias="X-Alertmanager-Signature"),
):
    """
    Webhook ingress for Prometheus Alertmanager notifications.
    Enforces HMAC-SHA256 signature verification and atomic PostgreSQL deduplication.
    """
    body_bytes = await request.body()
    signature = x_signature or x_alertmanager_signature

    # Verify HMAC in production or when secret is configured
    if settings.ALERTMANAGER_HMAC_SECRET:
        if not signature or not verify_alertmanager_hmac(body_bytes, signature, settings.ALERTMANAGER_HMAC_SECRET):
            logger.warning("Alertmanager webhook rejected: invalid or missing HMAC signature.")
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid HMAC signature.",
            )

    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Malformed JSON payload.",
        )

    alerts = payload.get("alerts", [])
    logger.info(f"Received authenticated Alertmanager webhook with {len(alerts)} alerts.")

    processed = []
    for alert in alerts:
        labels = alert.get("labels", {})
        annotations = alert.get("annotations", {})
        alert_name = labels.get("alertname", "UnknownAlert")
        service = labels.get("service") or labels.get("app") or labels.get("deployment") or "unknown-service"
        namespace = labels.get("namespace", "production")
        raw_severity = labels.get("severity", "warning").upper()
        severity = "CRITICAL" if raw_severity in ("CRITICAL", "PAGE", "FATAL") else "WARNING"

        # Resolve target kind
        target_kind = "Deployment"
        if "statefulset" in labels:
            target_kind = "StatefulSet"
            service = labels["statefulset"]
        elif "daemonset" in labels:
            target_kind = "DaemonSet"
            service = labels["daemonset"]

        # Atomic PostgreSQL Ingress Upsert
        result = await record_incident_signal(
            cluster_id=settings.CLUSTER_ID,
            namespace=namespace,
            target_kind=target_kind,
            target_name=service,
            severity=severity,
            source="ALERTMANAGER",
            signal_name=alert_name,
            raw_payload={
                "labels": labels,
                "annotations": annotations,
                "startsAt": alert.get("startsAt"),
                "status": alert.get("status"),
            },
        )

        processed.append({
            "incident_id": result["incident_id"],
            "is_new": result["is_new"],
            "signal_count": result["signal_count"],
            "target": f"{namespace}/{target_kind}/{service}",
            "state": result["state"],
        })

    return {
        "status": "accepted",
        "processed_count": len(processed),
        "incidents": processed,
    }
