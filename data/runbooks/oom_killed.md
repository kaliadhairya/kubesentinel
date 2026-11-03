# Runbook: Pod Terminated with OOMKilled (Exit Code 137)

## Symptoms
- Pod terminated with reason `OOMKilled`.
- Exit code `137` (SIGKILL dispatched by Linux kernel cgroup OOM killer).
- Memory working set metrics continuously rise until hitting container limit.
- Restart count increments periodically in `CrashLoopBackOff`.

## Diagnostic Steps
1. **Query Memory Trend**:
   `sum(container_memory_working_set_bytes{pod=~"$POD_NAME.*"}) by (pod)`
2. **Inspect Kernel Events**:
   `kubectl get events -n $NAMESPACE --field-selector reason=OOMKilled`
3. **Check Container Limit**:
   Verify `resources.limits.memory` in deployment specification.
4. **Identify Memory Leak vs Under-Provisioning**:
   - If memory rises monotonically over time: Code memory leak or cache buildup.
   - If memory spikes immediately on startup: Misconfigured JVM `-Xmx` or insufficient baseline heap.

## Approved Remediation Options
- **Option A (Rollback)**: If a new deployment occurred in the last 2 hours:
  `kubectl rollout undo deployment/$DEPLOYMENT_NAME -n $NAMESPACE`
- **Option B (Temporary Limit Increase)**: If traffic surged legitimately:
  Scale memory limit by 50% via targeted patch.
