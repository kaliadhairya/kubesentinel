# Runbook: Pod in CrashLoopBackOff (Exit Code 1 / Panic)

## Symptoms
- Pod phase is `Running` but container status is `Waiting` with reason `CrashLoopBackOff`.
- Exit code `1` or `2` (Application crash, uncaught exception, or fatal startup error).
- Restarts increment rapidly.

## Diagnostic Steps
1. **Fetch Previous Container Logs**:
   `kubectl logs $POD_NAME -n $NAMESPACE --previous --tail=100`
2. **Check Recent ConfigMap or Secret Mounts**:
   Verify if dependent credentials, DB endpoints, or API keys expired or were modified.
3. **Inspect Pod Readiness and Liveness Probes**:
   Look for `Unhealthy: Liveness probe failed` in `kubectl describe pod`.

## Approved Remediation Options
- **Option A (Rollback)**: If caused by recent binary release:
  `kubectl rollout undo deployment/$DEPLOYMENT_NAME -n $NAMESPACE`
- **Option B (Restart)**: If transient lock or database reconnect deadlock:
  `kubectl rollout restart deployment/$DEPLOYMENT_NAME -n $NAMESPACE`
