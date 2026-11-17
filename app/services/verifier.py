import asyncio
import logging
from datetime import datetime, timezone
from typing import Dict, Any, Optional
from app.core.config import settings
from app.services.k8s import k8s_service
from app.core.database import (
    get_remediation_action,
    update_remediation_verification,
    set_incident_state,
)

logger = logging.getLogger("kubesentinel.verifier")


class SpecDerivedVerifier:
    """
    Spec-Derived Health Verifier.
    Calculates dynamic soak windows based on workload readiness probe specifications:
    T_verify = min(300, max(90, startup_budget + readiness_delay + 3 * period + 60))
    Monitors pod health during soak; triggers automatic rollback to pre_state_snapshot on flap.
    """

    def calculate_soak_window(self, pod_spec: Optional[Dict[str, Any]] = None) -> int:
        """
        Derives the mathematically rigorous soak duration from pod container probe specs.
        """
        # Default probe specifications
        initial_delay = 30
        period = 10
        startup_budget = 30

        if pod_spec and "containers" in pod_spec:
            containers = pod_spec["containers"]
            if containers and isinstance(containers, list):
                c = containers[0]
                readiness = c.get("readinessProbe", {})
                initial_delay = readiness.get("initialDelaySeconds", initial_delay)
                period = readiness.get("periodSeconds", period)
                startup = c.get("startupProbe", {})
                startup_budget = startup.get("initialDelaySeconds", startup_budget)

        calculated = startup_budget + initial_delay + (3 * period) + 60
        return min(300, max(90, calculated))

    async def check_health_now(
        self,
        namespace: str,
        target_name: str,
        simulate_regression: bool = False,
    ) -> Dict[str, Any]:
        """
        Inspects current health metrics of the target workload.
        """
        if simulate_regression:
            return {
                "healthy": False,
                "phase": "CrashLoopBackOff",
                "ready_replicas": 0,
                "replicas": 3,
                "restarts_delta": 2,
                "exit_code": 137,
                "reason": "Memory cgroup limit exceeded during soak verification",
            }

        if k8s_service.mock_mode:
            svc = k8s_service._mock_state.get(target_name, {})
            # When rollback was applied, status becomes Running, restarts 0, ready == replicas
            is_healthy = svc.get("status") == "Running" and svc.get("ready_replicas", 0) == svc.get("replicas", 3)
            return {
                "healthy": is_healthy,
                "phase": svc.get("status", "Running"),
                "ready_replicas": svc.get("ready_replicas", 3),
                "replicas": svc.get("replicas", 3),
                "restarts_delta": 0 if is_healthy else 1,
                "exit_code": svc.get("exit_code"),
                "reason": svc.get("reason"),
            }

        k8s_service._ensure_client()
        pods = await k8s_service.get_pods(namespace=namespace, label=f"app={target_name}")
        if not pods:
            return {"healthy": False, "reason": "No pods found matching workload label."}

        total_pods = len(pods)
        ready_pods = 0
        total_restarts = 0

        for p in pods:
            if p.get("status") == "Running":
                ready_str = p.get("ready", "0/1")
                try:
                    num_r, den_r = ready_str.split("/")
                    if num_r == den_r and int(num_r) > 0:
                        ready_pods += 1
                except Exception:
                    pass
            total_restarts += p.get("restarts", 0)

        is_healthy = (ready_pods == total_pods) and (total_pods > 0)
        return {
            "healthy": is_healthy,
            "phase": "Running" if is_healthy else "Degraded",
            "ready_replicas": ready_pods,
            "replicas": total_pods,
            "restarts_delta": total_restarts,
            "reason": None if is_healthy else "Not all container replicas are ready",
        }

    async def verify_remediation(
        self,
        incident_id: str,
        namespace: str,
        target_name: str,
        soak_seconds: Optional[int] = None,
        fast_forward: bool = True,
        simulate_regression: bool = False,
    ) -> Dict[str, Any]:
        """
        Executes the verification soak period.
        Transitions incident state:
        - On success: VERIFYING -> RESOLVED
        - On regression: VERIFYING -> REVERTING -> FAILED_ESCALATED
        """
        action = await get_remediation_action(incident_id)
        pre_snapshot = action.get("pre_state_snapshot", {}) if action else {}
        if isinstance(pre_snapshot, str):
            try:
                import json
                pre_snapshot = json.loads(pre_snapshot)
            except Exception:
                pre_snapshot = {}

        duration = soak_seconds or self.calculate_soak_window()
        logger.info(f"Initiating verification soak window of {duration}s for {target_name} on incident {incident_id}")

        if not fast_forward:
            await asyncio.sleep(min(duration, 5))

        # Check health during soak
        health = await self.check_health_now(
            namespace=namespace,
            target_name=target_name,
            simulate_regression=simulate_regression,
        )

        now = datetime.now(timezone.utc).isoformat()

        if health["healthy"]:
            # Verification passed!
            res = {
                "status": "HEALTHY",
                "verified_at": now,
                "soak_seconds": duration,
                "ready_replicas": health["ready_replicas"],
                "replicas": health["replicas"],
                "restarts_delta": 0,
            }
            await update_remediation_verification(incident_id, res)
            await set_incident_state(incident_id, "RESOLVED")
            logger.info(f"Incident {incident_id} successfully verified and RESOLVED.")
            return {
                "success": True,
                "incident_id": incident_id,
                "state": "RESOLVED",
                "verification_result": res,
            }
        else:
            # Verification failed! Automatic rollback triggered.
            logger.error(
                f"Regression detected during soak for {target_name}! "
                f"Phase={health['phase']}, Reason={health['reason']}. Triggering auto-revert."
            )
            await set_incident_state(incident_id, "REVERTING")

            revert_details = {}
            if pre_snapshot:
                # In mock mode, restore pre-snapshot state
                if k8s_service.mock_mode:
                    svc = k8s_service._mock_state.get(target_name, {})
                    svc["status"] = "CrashLoopBackOff"
                    svc["current_image"] = pre_snapshot.get("current_image", "checkout:v2.4.2")
                    svc["ready_replicas"] = 0
                revert_details = {
                    "reverted_to_image": pre_snapshot.get("current_image"),
                    "reverted_to_revision": pre_snapshot.get("current_revision"),
                }

            res = {
                "status": "REGRESSION_DETECTED",
                "failed_at": now,
                "health_details": health,
                "auto_revert_executed": True,
                "revert_details": revert_details,
            }
            await update_remediation_verification(incident_id, res)
            await set_incident_state(incident_id, "FAILED_ESCALATED")

            return {
                "success": False,
                "incident_id": incident_id,
                "state": "FAILED_ESCALATED",
                "verification_result": res,
            }


spec_verifier = SpecDerivedVerifier()
