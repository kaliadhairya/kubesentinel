import json
import logging
from typing import Dict, Any, List, Optional
from app.core.config import settings
from app.core.guardrails import InfraGuardrails, GuardrailViolationException

logger = logging.getLogger("kubesentinel.k8s")


class KubernetesService:
    """
    Interacts with Kubernetes API via official kubernetes-client Python SDK
    or provides high-fidelity simulated cluster state for offline runs.
    """

    def __init__(self):
        self.mock_mode = settings.K8S_MOCK_MODE
        self._v1 = None
        self._apps_v1 = None

        # Simulated cluster state for offline runs & mock demos
        self.reset_mock_state()

    def reset_mock_state(self):
        """Resets simulated cluster state back to initial incident conditions."""
        self._mock_state = {
            "checkout-service": {
                "namespace": "production",
                "revision": 42,
                "previous_revision": 41,
                "current_image": "checkout:v2.4.2",
                "previous_image": "checkout:v2.4.1",
                "replicas": 3,
                "ready_replicas": 0,
                "status": "CrashLoopBackOff",
                "exit_code": 137,
                "reason": "OOMKilled",
                "restarts": 7,
                "memory_limit": "512Mi",
                "recent_commit": "a9f3b12 (feat: enable in-memory checkout session buffer)",
            }
        }

    def _ensure_client(self):
        """Initializes kubernetes-client with in-cluster SA or local kubeconfig fallback."""
        if self._v1 and self._apps_v1:
            return
        try:
            from kubernetes import client, config
            try:
                config.load_incluster_config()
                logger.info("Loaded in-cluster Kubernetes ServiceAccount configuration.")
            except Exception:
                config.load_kube_config()
                logger.info("Loaded local kubeconfig configuration.")
            self._v1 = client.CoreV1Api()
            self._apps_v1 = client.AppsV1Api()
        except Exception as e:
            logger.warning(f"Could not initialize Kubernetes client SDK ({e}). Defaulting to mock engine.")
            self.mock_mode = True

    async def get_pods(self, namespace: str = "production", label: Optional[str] = None) -> List[Dict[str, Any]]:
        """Lists pods in namespace matching optional label."""
        InfraGuardrails.validate_command("kubectl get pods", namespace=namespace)

        if self.mock_mode:
            svc = self._mock_state.get("checkout-service", {})
            return [
                {
                    "name": f"checkout-7d4bf6-x9z{i+1}",
                    "namespace": namespace,
                    "status": svc.get("status", "Running"),
                    "ready": f"{svc.get('ready_replicas', 0)}/{svc.get('replicas', 3)}",
                    "restarts": svc.get("restarts", 0),
                    "age": "14m",
                    "node": f"worker-node-0{i+1}",
                }
                for i in range(svc.get("replicas", 3))
            ]

        self._ensure_client()
        kwargs = {}
        if label:
            kwargs["label_selector"] = label
        pod_list = self._v1.list_namespaced_pod(namespace=namespace, **kwargs)
        result = []
        for p in pod_list.items:
            restarts = 0
            ready_count = 0
            total_containers = len(p.status.container_statuses or [])
            if p.status.container_statuses:
                for c in p.status.container_statuses:
                    restarts += c.restart_count
                    if c.ready:
                        ready_count += 1
            result.append({
                "name": p.metadata.name,
                "namespace": p.metadata.namespace,
                "status": p.status.phase,
                "ready": f"{ready_count}/{total_containers}",
                "restarts": restarts,
                "age": "unknown",
                "node": p.spec.node_name or "unassigned",
            })
        return result

    async def describe_pod(self, namespace: str, pod_name: str) -> Dict[str, Any]:
        """Provides detailed inspection of pod phase, exit codes, and conditions."""
        InfraGuardrails.validate_command(f"kubectl describe pod {pod_name}", namespace=namespace)

        if self.mock_mode:
            svc = self._mock_state.get("checkout-service", {})
            return {
                "name": pod_name,
                "namespace": namespace,
                "phase": "Running",
                "container_statuses": [
                    {
                        "container": "checkout",
                        "image": svc.get("current_image", "checkout:v2.4.2"),
                        "ready": svc.get("ready_replicas", 0) > 0,
                        "restart_count": svc.get("restarts", 7),
                        "state": {
                            "waiting": {
                                "reason": svc.get("status", "CrashLoopBackOff"),
                                "message": "back-off 5m0s restarting failed container=checkout",
                            }
                        },
                        "last_state": {
                            "terminated": {
                                "exit_code": svc.get("exit_code", 137),
                                "reason": svc.get("reason", "OOMKilled"),
                                "finished_at": "2026-11-03T06:12:00Z",
                            }
                        },
                    }
                ],
                "resource_limits": {
                    "cpu": "500m",
                    "memory": svc.get("memory_limit", "512Mi"),
                },
            }

        self._ensure_client()
        pod = self._v1.read_namespaced_pod(name=pod_name, namespace=namespace)
        containers_info = []
        for c in (pod.status.container_statuses or []):
            term = c.last_state.terminated if c.last_state else None
            waiting = c.state.waiting if c.state else None
            containers_info.append({
                "container": c.name,
                "image": c.image,
                "ready": c.ready,
                "restart_count": c.restart_count,
                "state": {
                    "waiting": {"reason": waiting.reason, "message": waiting.message} if waiting else None
                },
                "last_state": {
                    "terminated": {
                        "exit_code": term.exit_code,
                        "reason": term.reason,
                        "finished_at": str(term.finished_at) if term else None,
                    } if term else None
                },
            })

        limits = {}
        if pod.spec.containers:
            res = pod.spec.containers[0].resources
            if res and res.limits:
                limits = {"cpu": res.limits.get("cpu", "unlimited"), "memory": res.limits.get("memory", "unlimited")}

        return {
            "name": pod.metadata.name,
            "namespace": pod.metadata.namespace,
            "phase": pod.status.phase,
            "container_statuses": containers_info,
            "resource_limits": limits,
        }

    async def get_events(self, namespace: str = "production") -> List[Dict[str, Any]]:
        """Retrieves recent cluster events for the given namespace."""
        InfraGuardrails.validate_command("kubectl get events", namespace=namespace)

        if self.mock_mode:
            svc = self._mock_state.get("checkout-service", {})
            return [
                {
                    "type": "Warning",
                    "reason": svc.get("reason", "OOMKilled"),
                    "object": "pod/checkout-7d4bf6-x9z1",
                    "message": "Memory cgroup out of memory: Killed process 4102 (java) total-vm:1082400kB, anon-rss:524288kB, file-rss:0kB",
                    "count": svc.get("restarts", 7),
                    "last_timestamp": "2m ago",
                },
                {
                    "type": "Warning",
                    "reason": "BackOff",
                    "object": "pod/checkout-7d4bf6-x9z1",
                    "message": "Back-off restarting failed container",
                    "count": svc.get("restarts", 7),
                    "last_timestamp": "1m ago",
                },
            ]

        self._ensure_client()
        events_list = self._v1.list_namespaced_event(namespace=namespace)
        items = []
        for e in events_list.items:
            items.append({
                "type": e.type,
                "reason": e.reason,
                "object": f"{e.involved_object.kind}/{e.involved_object.name}" if e.involved_object else "unknown",
                "message": e.message,
                "count": e.count or 1,
                "last_timestamp": str(e.last_timestamp or e.event_time or "recent"),
            })
        return items

    async def get_pod_logs(
        self,
        namespace: str,
        pod_name: str,
        previous: bool = True,
        tail: int = 100,
    ) -> str:
        """Fetches container logs using official SDK, supporting previous crashed instance tail."""
        InfraGuardrails.validate_command(f"kubectl logs {pod_name}", namespace=namespace)

        if self.mock_mode:
            return (
                "2026-11-03 06:11:45.102 INFO  [main] c.s.CheckoutApp: In-memory session buffer initialized (target=unbounded)\n"
                "2026-11-03 06:11:52.418 INFO  [http-nio-8080] c.s.session.SessionStore: Ingested 12,400 active cart sessions\n"
                "2026-11-03 06:11:58.892 WARN  [http-nio-8080] c.s.session.LocalCache: JVM OldGen usage exceeded 90% (462Mi / 512Mi)\n"
                "2026-11-03 06:12:00.012 ERROR [http-nio-8080] c.s.session.LocalCache: Exception in thread 'worker-3': java.lang.OutOfMemoryError: Java heap space\n"
                "\tat com.store.session.LocalCache.put(LocalCache.java:88)\n"
                "\tat com.store.checkout.CartService.processCart(CartService.java:142)\n"
                "2026-11-03 06:12:00.215 FATAL [main] c.s.CheckoutApp: Kernel SIGKILL (Exit 137) caught. Process terminated."
            )

        self._ensure_client()
        try:
            logs = self._v1.read_namespaced_pod_log(
                name=pod_name,
                namespace=namespace,
                previous=previous,
                tail_lines=tail,
            )
            return logs
        except Exception as e:
            logger.warning(f"Failed to fetch previous logs for {pod_name}: {e}. Retrying without previous=True")
            return self._v1.read_namespaced_pod_log(
                name=pod_name,
                namespace=namespace,
                previous=False,
                tail_lines=tail,
            )

    async def get_rollouts(self, namespace: str, deployment_name: str) -> Dict[str, Any]:
        """Inspects recent GitOps/Kubernetes rollout revisions and commits."""
        InfraGuardrails.validate_command(f"kubectl rollout history deployment/{deployment_name}", namespace=namespace)

        if self.mock_mode:
            svc = self._mock_state.get(deployment_name, {})
            return {
                "deployment": deployment_name,
                "namespace": namespace,
                "current_revision": svc.get("revision", 42),
                "current_image": svc.get("current_image", "checkout:v2.4.2"),
                "previous_revision": svc.get("previous_revision", 41),
                "previous_image": svc.get("previous_image", "checkout:v2.4.1"),
                "deployed_at": "14 minutes ago",
                "commit": svc.get("recent_commit", "a9f3b12"),
            }

        self._ensure_client()
        dep = self._apps_v1.read_namespaced_deployment(name=deployment_name, namespace=namespace)
        annotations = dep.metadata.annotations or {}
        curr_rev = annotations.get("deployment.kubernetes.io/revision", "1")
        image = dep.spec.template.spec.containers[0].image if dep.spec.template.spec.containers else "unknown"

        # Query replicasets to find previous revision
        rs_list = self._apps_v1.list_namespaced_replica_set(namespace=namespace)
        matching_rs = [
            rs for rs in rs_list.items
            if any(ref.name == deployment_name for ref in (rs.metadata.owner_references or []))
        ]
        matching_rs.sort(
            key=lambda rs: int((rs.metadata.annotations or {}).get("deployment.kubernetes.io/revision", "0")),
            reverse=True,
        )

        prev_rev = curr_rev
        prev_image = image
        if len(matching_rs) > 1:
            prev_rs = matching_rs[1]
            prev_rev = (prev_rs.metadata.annotations or {}).get("deployment.kubernetes.io/revision", "1")
            if prev_rs.spec.template.spec.containers:
                prev_image = prev_rs.spec.template.spec.containers[0].image

        return {
            "deployment": deployment_name,
            "namespace": namespace,
            "current_revision": int(curr_rev),
            "current_image": image,
            "previous_revision": int(prev_rev),
            "previous_image": prev_image,
            "deployed_at": str(dep.metadata.creation_timestamp),
            "commit": annotations.get("app.kubernetes.io/commit", "HEAD"),
        }

    async def rollout_undo(self, namespace: str, deployment_name: str) -> Dict[str, Any]:
        """Rolls back deployment to previous stable revision using Kubernetes API patch."""
        cmd_str = f"kubectl rollout undo deployment/{deployment_name}"
        InfraGuardrails.validate_command(cmd_str, namespace=namespace)

        if self.mock_mode:
            svc = self._mock_state.get(deployment_name, {})
            prev_rev = svc.get("previous_revision", 41)
            prev_img = svc.get("previous_image", "checkout:v2.4.1")
            svc["revision"] = 43
            svc["current_image"] = prev_img
            svc["status"] = "Running"
            svc["ready_replicas"] = svc.get("replicas", 3)
            svc["restarts"] = 0
            svc["reason"] = None
            svc["exit_code"] = None

            return {
                "success": True,
                "command": cmd_str,
                "namespace": namespace,
                "target": deployment_name,
                "restored_revision": prev_rev,
                "active_image": prev_img,
                "status": "Deployment successfully rolled back. 3/3 replicas healthy and ready.",
            }

        self._ensure_client()
        # Find previous replica set pod template spec
        rollout_info = await self.get_rollouts(namespace, deployment_name)
        target_image = rollout_info["previous_image"]

        patch = {
            "spec": {
                "template": {
                    "spec": {
                        "containers": [
                            {"name": deployment_name, "image": target_image}
                        ]
                    }
                }
            }
        }
        self._apps_v1.patch_namespaced_deployment(name=deployment_name, namespace=namespace, body=patch)
        return {
            "success": True,
            "command": cmd_str,
            "namespace": namespace,
            "target": deployment_name,
            "restored_revision": rollout_info["previous_revision"],
            "active_image": target_image,
            "status": f"Patched deployment {deployment_name} to restore image {target_image}.",
        }


k8s_service = KubernetesService()
