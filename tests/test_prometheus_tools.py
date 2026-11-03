import pytest
from app.services.prometheus import prometheus_service


@pytest.mark.asyncio
async def test_promql_instant_query():
    """Validates evaluating an instant PromQL query."""
    res = await prometheus_service.query_instant(
        "sum(rate(http_requests_total{status=~'5..'}[5m]))"
    )
    assert res["resultType"] == "vector"
    assert len(res["result"]) > 0
    assert float(res["result"][0]["value"][1]) > 0


@pytest.mark.asyncio
async def test_promql_range_query():
    """Validates range query time-series normalization for charts."""
    res = await prometheus_service.query_range(
        query="sum(container_memory_working_set_bytes{service='checkout'})",
        start_seconds_ago=1800,
    )
    assert "points" in res
    assert len(res["points"]) > 0
    assert "summary" in res
    assert res["summary"]["max"] >= 400
