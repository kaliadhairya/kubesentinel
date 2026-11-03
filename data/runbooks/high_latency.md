# Runbook: High Latency & HTTP 5xx Error Rate Spike

## Symptoms
- HTTP 502 / 503 / 504 surges on ingress or internal mesh.
- P99 latency exceeds SLO threshold (> 2.0s).
- Downstream microservice queue buildup.

## Diagnostic Steps
1. **Query RED Metrics**:
   - Error Rate: `sum(rate(http_requests_total{status=~"5.."}[5m])) / sum(rate(http_requests_total[5m]))`
   - P99 Latency: `histogram_quantile(0.99, sum(rate(http_request_duration_seconds_bucket[5m])) by (le))`
2. **Inspect Upstream vs Downstream Callers**:
   Trace dependency graphs to identify deepest bottleneck.
3. **Check CPU Throttling**:
   `rate(container_cpu_cfs_throttled_periods_total[5m]) / rate(container_cpu_cfs_periods_total[5m])`

## Approved Remediation Options
- **Option A (Horizontal Scale)**: Increase replica count to relieve immediate queue pressure:
  `kubectl scale deployment/$DEPLOYMENT_NAME --replicas=$NEW_REPLICAS -n $NAMESPACE`
- **Option B (Drain / Restart Unhealthy Instances)**: If specific pods are degraded or hung.
