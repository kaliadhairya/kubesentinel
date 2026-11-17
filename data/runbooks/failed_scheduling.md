# Runbook: Pod in FailedScheduling (Pending State)

## Symptoms
* Pod remains in `Pending` phase without being assigned to a worker node (`nodeName` is empty).
* Warning Event `FailedScheduling`: `0/N nodes are available: insufficient cpu`, `insufficient memory`, or `node(s) had untolerated taint`.

## Root Causes
1. **Cluster Resource Exhaustion:** Total requested CPU/memory on the cluster exceeds capacity.
2. **Excessive Resource Requests:** Deployment sets unrealistic requests (e.g., `requests.memory: 64Gi`).
3. **Node Taints & Tolerations / NodeAffinity:** Pod requires a specific node label or lacks tolerations for specialized node taints.

## Diagnostic Steps
1. Run `kubectl get events -n <namespace>` and filter on `FailedScheduling`.
2. Inspect `spec.containers[*].resources.requests`.
3. Check node allocatable resources with `kubectl describe nodes`.

## Remediation
* If resources are over-requested, update deployment requests via GitOps PR or scale up cluster nodes.
* If pod has invalid nodeSelector, patch manifest to target available nodes.
