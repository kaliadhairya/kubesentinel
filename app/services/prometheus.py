import time
import math
import logging
from typing import Dict, Any, List, Optional
import httpx
from app.core.config import settings
from app.core.guardrails import InfraGuardrails

logger = logging.getLogger("kubesentinel.prometheus")


class PrometheusService:
    """Evaluates PromQL queries against live Prometheus or high-fidelity simulated metrics."""

    def __init__(self):
        self.mock_mode = settings.PROMETHEUS_MOCK_MODE
        self.base_url = settings.PROMETHEUS_URL.rstrip("/")

    async def query_instant(self, query: str) -> Dict[str, Any]:
        """Executes an instant PromQL query at current timestamp."""
        sanitized_query = InfraGuardrails.sanitize_promql(query)

        if self.mock_mode:
            return self._mock_instant_query(sanitized_query)

        url = f"{self.base_url}/api/v1/query"
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url, params={"query": sanitized_query})
            resp.raise_for_status()
            return resp.json().get("data", {})

    async def query_range(
        self,
        query: str,
        start_seconds_ago: int = 1800,
        step: str = "30s",
    ) -> Dict[str, Any]:
        """Executes a range PromQL query and returns formatted time-series points."""
        sanitized_query = InfraGuardrails.sanitize_promql(query)
        end_time = int(time.time())
        start_time = end_time - start_seconds_ago

        if self.mock_mode:
            return self._mock_range_query(sanitized_query, start_time, end_time)

        url = f"{self.base_url}/api/v1/query_range"
        params = {
            "query": sanitized_query,
            "start": start_time,
            "end": end_time,
            "step": step,
        }
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(url, params=params)
            resp.raise_for_status()
            data = resp.json().get("data", {})
            return self._format_uplot_timeseries(data, sanitized_query)

    def _mock_instant_query(self, query: str) -> Dict[str, Any]:
        """Returns realistic mock instant values based on query semantics."""
        q = query.lower()
        if "5.." in q or "error" in q:
            return {
                "resultType": "vector",
                "result": [
                    {
                        "metric": {"service": "checkout-service", "status": "500"},
                        "value": [time.time(), "0.184"],  # 18.4% error rate
                    }
                ],
            }
        elif "memory" in q:
            return {
                "resultType": "vector",
                "result": [
                    {
                        "metric": {"pod": "checkout-7d4bf6-x9z1", "container": "checkout"},
                        "value": [time.time(), "536870912"],  # 512 MiB limit
                    }
                ],
            }
        else:
            return {
                "resultType": "vector",
                "result": [
                    {
                        "metric": {"service": "checkout-service"},
                        "value": [time.time(), "1.42"],
                    }
                ],
            }

    def _mock_range_query(self, query: str, start_time: int, end_time: int) -> Dict[str, Any]:
        """Synthesizes high-fidelity time-series showing an incident escalation."""
        q = query.lower()
        timestamps = list(range(start_time, end_time + 1, 60))
        series_data = []

        is_memory = "memory" in q or "working_set" in q
        is_error = "5.." in q or "status" in q or "error" in q
        incident_start = end_time - 900  # Incident began 15 mins ago

        for ts in timestamps:
            if is_memory:
                if ts < incident_start:
                    val = 180 + math.sin(ts / 100) * 10  # Baseline ~180MB
                else:
                    elapsed = ts - incident_start
                    # Monotonic memory leak climbing to 512MB limit, crashing and repeating
                    cycle = (elapsed % 300) / 300
                    val = 200 + cycle * 312
                series_data.append([ts, round(val, 2)])
                metric_name = "container_memory_working_set_bytes (MB)"
            elif is_error:
                if ts < incident_start:
                    val = 0.01 + math.sin(ts / 50) * 0.005  # 0.01% baseline
                else:
                    val = 15.0 + math.sin(ts / 40) * 4.0  # Spikes to ~18%
                series_data.append([ts, round(val, 3)])
                metric_name = "http_5xx_error_percentage (%)"
            else:
                # Latency
                if ts < incident_start:
                    val = 120 + math.sin(ts / 60) * 15  # 120ms baseline
                else:
                    val = 3800 + math.sin(ts / 30) * 600  # 4s p99 latency
                series_data.append([ts, round(val, 1)])
                metric_name = "http_p99_latency_ms (ms)"

        return {
            "query": query,
            "metric_name": metric_name,
            "points": series_data,
            "summary": {
                "min": min(p[1] for p in series_data),
                "max": max(p[1] for p in series_data),
                "latest": series_data[-1][1],
            },
        }

    def _format_uplot_timeseries(self, raw_data: Dict[str, Any], query: str) -> Dict[str, Any]:
        """Normalizes live Prometheus range metrics into structured points."""
        results = raw_data.get("result", [])
        if not results:
            return {"query": query, "metric_name": "No Data", "points": [], "summary": {}}

        first_metric = results[0]
        points = [[int(pt[0]), float(pt[1])] for pt in first_metric.get("values", [])]

        return {
            "query": query,
            "metric_name": first_metric.get("metric", {}).get("__name__", "metric"),
            "points": points,
            "summary": {
                "min": min(p[1] for p in points) if points else 0,
                "max": max(p[1] for p in points) if points else 0,
                "latest": points[-1][1] if points else 0,
            },
        }


prometheus_service = PrometheusService()
