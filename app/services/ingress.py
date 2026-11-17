import hmac
import hashlib
import json
import logging
import asyncio
import random
from typing import Optional, Tuple, Dict, Any, List
from app.core.config import settings
from app.core.database import record_incident_signal

logger = logging.getLogger("kubesentinel.ingress")


def verify_alertmanager_hmac(
    body: bytes,
    signature_header: Optional[str],
    secret: str = settings.ALERTMANAGER_HMAC_SECRET,
) -> bool:
    """
    Verifies HMAC-SHA256 signature for Alertmanager incoming webhooks.
    Accepts hex string or 'sha256=<hex>'.
    """
    if not secret:
        logger.warning("ALERTMANAGER_HMAC_SECRET not set; rejecting unauthenticated webhook.")
        return False

    if not signature_header:
        return False

    expected_sig = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    provided_sig = signature_header.strip()
    if provided_sig.startswith("sha256="):
        provided_sig = provided_sig[7:]

    return hmac.compare_digest(expected_sig, provided_sig)


class OwnerReferenceResolver:
    """
    Recursively resolves Kubernetes OwnerReferences up to the top-level controller
    (Pod -> ReplicaSet -> Deployment, Job -> CronJob, StatefulSet, DaemonSet, Argo Rollout).
    """

    @staticmethod
    def resolve_from_dict(
        kind: str,
        name: str,
        owner_references: Optional[List[Dict[str, Any]]] = None,
        get_owner_fn: Optional[Any] = None,
    ) -> Tuple[str, str]:
        """
        Pure recursive resolution given kind, name, and owner_references list.
        """
        if not owner_references:
            return kind, name

        # Prioritize controller reference
        primary_owner = None
        for ref in owner_references:
            if ref.get("controller"):
                primary_owner = ref
                break
        if not primary_owner and owner_references:
            primary_owner = owner_references[0]

        if not primary_owner:
            return kind, name

        owner_kind = primary_owner.get("kind", kind)
        owner_name = primary_owner.get("name", name)

        # Standard controller unwraps
        if owner_kind == "ReplicaSet":
            if get_owner_fn:
                parent_kind, parent_name = get_owner_fn(owner_kind, owner_name)
                if parent_kind and parent_name:
                    return parent_kind, parent_name
            # Fallback heuristic: ReplicaSet is named <deployment>-<pod-template-hash>
            parts = owner_name.rsplit("-", 1)
            if len(parts) == 2 and 4 <= len(parts[1]) <= 12 and parts[1].isalnum():
                return "Deployment", parts[0]
            return "Deployment", owner_name

        if owner_kind == "Job":
            if get_owner_fn:
                parent_kind, parent_name = get_owner_fn(owner_kind, owner_name)
                if parent_kind and parent_name:
                    return parent_kind, parent_name
            parts = owner_name.rsplit("-", 1)
            if len(parts) == 2 and parts[1].isdigit():
                return "CronJob", parts[0]
            return "Job", owner_name

        if owner_kind in ("Deployment", "StatefulSet", "DaemonSet", "CronJob", "Rollout"):
            return owner_kind, owner_name

        return owner_kind, owner_name


class K8sEventWatcher:
    """
    Event-driven Kubernetes Informer / Watcher using kubernetes-client.
    Monitors pod terminations and warning events with 410 Gone relist recovery.
    """

    def __init__(self):
        self._running = False
        self._task: Optional[asyncio.Task] = None

    async def start(self):
        """Starts the background watch loop."""
        if self._running:
            return
        self._running = True
        if settings.K8S_MOCK_MODE:
            logger.info("K8sEventWatcher initialized in MOCK mode (cluster watch simulated).")
            return
        self._task = asyncio.create_task(self._watch_loop())
        logger.info("K8sEventWatcher background task started.")

    async def stop(self):
        """Stops the watch loop gracefully."""
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("K8sEventWatcher stopped.")

    async def _watch_loop(self):
        """Watches Pod and Event streams with 410 Gone recovery and backoff."""
        backoff_sec = 1.0
        resource_version: Optional[str] = None

        while self._running:
            try:
                from kubernetes import client, config, watch
                try:
                    config.load_incluster_config()
                except Exception:
                    config.load_kube_config()

                v1 = client.CoreV1Api()
                w = watch.Watch()

                logger.info(f"Connecting to K8s Watch stream (resourceVersion={resource_version})...")
                backoff_sec = 1.0  # Reset on successful connection

                # Stream pod phase changes and container terminations
                stream_kwargs = {"timeout_seconds": 60}
                if resource_version:
                    stream_kwargs["resource_version"] = resource_version

                # Run blocking watch iteration in thread pool to prevent blocking event loop
                loop = asyncio.get_running_loop()
                events = await loop.run_in_executor(
                    None,
                    lambda: list(w.stream(v1.list_pod_for_all_namespaces, **stream_kwargs))
                )

                for event in events:
                    if not self._running:
                        break
                    pod = event.get("object")
                    if not pod:
                        continue

                    resource_version = pod.metadata.resource_version
                    event_type = event.get("type")

                    # Inspect container termination exit codes
                    ns = pod.metadata.namespace
                    pod_name = pod.metadata.name

                    # Filter namespace guardrail
                    if ns not in settings.allowed_namespaces:
                        continue

                    status = pod.status
                    if not status or not status.container_statuses:
                        continue

                    for cs in status.container_statuses:
                        terminated = cs.last_state.terminated if cs.last_state else None
                        waiting = cs.state.waiting if cs.state else None

                        is_crash = False
                        reason = "Unknown"
                        exit_code = 0

                        if terminated and terminated.exit_code != 0:
                            is_crash = True
                            exit_code = terminated.exit_code
                            reason = terminated.reason or f"ExitCode:{exit_code}"
                        elif waiting and waiting.reason in ("CrashLoopBackOff", "OOMKilled", "ImagePullBackOff"):
                            is_crash = True
                            reason = waiting.reason

                        if is_crash:
                            owners = [
                                {"kind": r.kind, "name": r.name, "controller": r.controller}
                                for r in (pod.metadata.owner_references or [])
                            ]
                            target_kind, target_name = OwnerReferenceResolver.resolve_from_dict(
                                "Pod", pod_name, owners
                            )

                            logger.warning(
                                f"Watcher detected crash on {ns}/{target_kind}/{target_name} "
                                f"(Pod: {pod_name}, Reason: {reason}, Exit: {exit_code})"
                            )

                            await record_incident_signal(
                                cluster_id=settings.CLUSTER_ID,
                                namespace=ns,
                                target_kind=target_kind,
                                target_name=target_name,
                                severity="CRITICAL" if exit_code == 137 or "OOM" in reason else "WARNING",
                                source="K8S_WATCH",
                                signal_name=f"ContainerCrash:{reason}",
                                raw_payload={
                                    "pod_name": pod_name,
                                    "container": cs.name,
                                    "exit_code": exit_code,
                                    "reason": reason,
                                    "restarts": cs.restart_count,
                                },
                            )

            except Exception as e:
                err_str = str(e)
                if "410" in err_str or "Expired" in err_str:
                    logger.warning("K8s Watch resourceVersion expired (410 Gone). Relisting from scratch.")
                    resource_version = None
                else:
                    logger.error(f"K8s Watch stream error: {e}. Reconnecting in {backoff_sec:.1f}s...")
                    await asyncio.sleep(backoff_sec + random.uniform(0.1, 0.5))
                    backoff_sec = min(backoff_sec * 2.0, 30.0)


k8s_event_watcher = K8sEventWatcher()
