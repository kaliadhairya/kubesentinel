import json
import logging
import asyncio
from typing import Dict, Any, List, Optional
from app.core.config import settings
from app.core.guardrails import InfraGuardrails, GuardrailViolationException

logger = logging.getLogger("kubesentinel.k8s")


class KubernetesService:
    """Interacts with Kubernetes API or provides high-fidelity simulated cluster state."""

    def __init__(self):
        self.mock_mode = settings.K8S_MOCK_MODE
        # Mock cluster state
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

        # Live kubectl execution
        cmd = ["kubectl", "get", "pods", "-n", namespace, "-o", "json"]
        if label:
            cmd.extend(["-l", label])
        return await self._run_kubectl_json(cmd)

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

        cmd = ["kubectl", "get", "pod", pod_name, "-n", namespace, "-o", "json"]
        return await self._run_kubectl_json(cmd)

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

        cmd = ["kubectl", "get", "events", "-n", namespace, "--sort-by=.lastTimestamp", "-o", "json"]
        res = await self._run_kubectl_json(cmd)
        return res.get("items", [])

    async def get_pod_logs(
        self,
        namespace: str,
        pod_name: str,
        previous: bool = True,
        tail: int = 100,
    ) -> str:
        """Fetches container logs, supporting previous crashed instance tail."""
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

        cmd = ["kubectl", "logs", pod_name, "-n", namespace, f"--tail={tail}"]
        if previous:
            cmd.append("--previous")

        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await proc.communicate()
        return stdout.decode("utf-8") if stdout else stderr.decode("utf-8")

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

        cmd = ["kubectl", "rollout", "history", f"deployment/{deployment_name}", "-n", namespace, "-o", "json"]
        return await self._run_kubectl_json(cmd)

    async def rollout_undo(self, namespace: str, deployment_name: str) -> Dict[str, Any]:
        """Rolls back deployment to previous stable revision (Mutating - Requires Approval)."""
        cmd_str = f"kubectl rollout undo deployment/{deployment_name}"
        InfraGuardrails.validate_command(cmd_str, namespace=namespace)

        if self.mock_mode:
            svc = self._mock_state.get(deployment_name, {})
            # Execute simulated rollback
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

        cmd = ["kubectl", "rollout", "undo", f"deployment/{deployment_name}", "-n", namespace]
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await proc.communicate()
        return {
            "success": proc.returncode == 0,
            "output": stdout.decode("utf-8") if stdout else stderr.decode("utf-8"),
        }

    async def _run_kubectl_json(self, cmd: List[str]) -> Any:
        """Executes a kubectl command and parses JSON output."""
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            err = stderr.decode("utf-8")
            raise RuntimeError(f"Kubectl execution failed: {err}")
        return json.loads(stdout.decode("utf-8"))


k8s_service = KubernetesService()
