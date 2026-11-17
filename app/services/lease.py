import logging
from typing import Optional, Dict, Any
from app.core.database import (
    claim_incident_lease,
    renew_incident_lease,
    reap_stale_leases,
)

logger = logging.getLogger("kubesentinel.lease")


class WorkerLeaseManager:
    """Manages PostgreSQL FOR UPDATE SKIP LOCKED job claiming with heartbeat renewals."""

    def __init__(self, worker_id: str, lease_duration_sec: int = 30):
        self.worker_id = worker_id
        self.lease_duration_sec = lease_duration_sec

    async def claim_next_incident(self) -> Optional[Dict[str, Any]]:
        """Claims the highest-priority pending incident not currently locked by another worker."""
        return await claim_incident_lease(
            worker_id=self.worker_id,
            lease_duration_sec=self.lease_duration_sec,
        )

    async def heartbeat(self, incident_id: str) -> bool:
        """Renews active lease to prevent stale reaping during long investigation hops."""
        return await renew_incident_lease(
            incident_id=incident_id,
            worker_id=self.worker_id,
            extension_sec=self.lease_duration_sec,
        )

    async def reap_stale(self, max_attempts: int = 3) -> int:
        """Reaps expired leases and escalates incidents that have exceeded max retries."""
        return await reap_stale_leases(max_attempts=max_attempts)
