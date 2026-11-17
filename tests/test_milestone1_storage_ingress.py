import hmac
import hashlib
import json
import asyncio
import pytest
from httpx import AsyncClient, ASGITransport
from app.main import app
from app.core.config import settings
from app.core.database import (
    get_db_pool,
    record_incident_signal,
    claim_incident_lease,
    renew_incident_lease,
    reap_stale_leases,
    in_memory_store,
)
from app.services.ingress import OwnerReferenceResolver, verify_alertmanager_hmac
from app.services.lease import WorkerLeaseManager


@pytest.mark.asyncio
async def test_owner_reference_recursive_resolution():
    """Validates recursive unwrapping of Pod -> ReplicaSet -> Deployment and Job -> CronJob."""
    # 1. Pod -> ReplicaSet -> Deployment
    pod_rs_owner = [{"kind": "ReplicaSet", "name": "checkout-service-7d9f8b", "controller": True}]
    kind, name = OwnerReferenceResolver.resolve_from_dict("Pod", "checkout-service-7d9f8b-xk9z1", pod_rs_owner)
    assert kind == "Deployment"
    assert name == "checkout-service"

    # 2. Pod -> Job -> CronJob
    pod_job_owner = [{"kind": "Job", "name": "nightly-backup-1718293", "controller": True}]
    kind, name = OwnerReferenceResolver.resolve_from_dict("Pod", "nightly-backup-1718293-p4k1", pod_job_owner)
    assert kind == "CronJob"
    assert name == "nightly-backup"

    # 3. Pod -> StatefulSet
    pod_sts_owner = [{"kind": "StatefulSet", "name": "kafka-broker", "controller": True}]
    kind, name = OwnerReferenceResolver.resolve_from_dict("Pod", "kafka-broker-0", pod_sts_owner)
    assert kind == "StatefulSet"
    assert name == "kafka-broker"

    # 4. Standalone Pod (no owners)
    kind, name = OwnerReferenceResolver.resolve_from_dict("Pod", "debug-shell", [])
    assert kind == "Pod"
    assert name == "debug-shell"


@pytest.mark.asyncio
async def test_single_active_target_concurrency_deduplication():
    """
    INVARIANT I1 Verification:
    20 concurrent pod-crash signals for the same workload must collapse into exactly 1 active incident.
    """
    in_memory_store.incidents.clear()
    in_memory_store.incident_events.clear()
    pool = await get_db_pool()
    if pool:
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM incidents WHERE cluster_id = 'test-cluster';")

    async def emit_signal(i: int):
        return await record_incident_signal(
            cluster_id="test-cluster",
            namespace="production",
            target_kind="Deployment",
            target_name="checkout-service",
            severity="CRITICAL" if i == 5 else "WARNING",
            source="K8S_WATCH",
            signal_name="CrashLoopBackOff",
            raw_payload={"pod_name": f"checkout-pod-{i}", "exit_code": 137},
        )

    # Fire 20 signals concurrently
    results = await asyncio.gather(*(emit_signal(i) for i in range(20)))

    # All 20 signals must return the exact same incident_id
    incident_ids = {r["incident_id"] for r in results}
    assert len(incident_ids) == 1, f"Expected 1 unique incident, got {len(incident_ids)}"

    first_incident_id = results[0]["incident_id"]
    # Check that is_new was True exactly once
    new_flags = [r["is_new"] for r in results]
    assert sum(new_flags) == 1

    # Signal count must be 20
    final_result = await record_incident_signal(
        cluster_id="test-cluster",
        namespace="production",
        target_kind="Deployment",
        target_name="checkout-service",
        severity="WARNING",
        source="K8S_WATCH",
        signal_name="CrashLoopBackOff",
        raw_payload={"pod_name": "checkout-pod-21"},
    )
    assert final_result["signal_count"] == 21
    assert final_result["incident_id"] == first_incident_id


@pytest.mark.asyncio
async def test_distinct_workloads_create_separate_incidents():
    """Distinct workloads must never be erroneously merged."""
    in_memory_store.incidents.clear()
    pool = await get_db_pool()
    if pool:
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM incident_events; DELETE FROM incidents;")
    res1 = await record_incident_signal(
        cluster_id="test-cluster",
        namespace="production",
        target_kind="Deployment",
        target_name="checkout-service",
        severity="WARNING",
        source="ALERTMANAGER",
        signal_name="KubePodCrashLooping",
        raw_payload={},
    )
    res2 = await record_incident_signal(
        cluster_id="test-cluster",
        namespace="production",
        target_kind="Deployment",
        target_name="payment-service",
        severity="CRITICAL",
        source="ALERTMANAGER",
        signal_name="High5xxRate",
        raw_payload={},
    )
    assert res1["incident_id"] != res2["incident_id"]


@pytest.mark.asyncio
async def test_hmac_alertmanager_webhook_verification():
    """Verifies HMAC signature checking on Alertmanager webhooks."""
    secret = "test-secret-key-12345"
    payload = {
        "version": "4",
        "alerts": [
            {
                "status": "firing",
                "labels": {
                    "alertname": "PodCrashLooping",
                    "deployment": "order-service",
                    "namespace": "production",
                    "severity": "critical",
                },
                "annotations": {"summary": "Order service container crashing"},
            }
        ],
    }
    body_bytes = json.dumps(payload).encode("utf-8")
    valid_sig = hmac.new(secret.encode("utf-8"), body_bytes, hashlib.sha256).hexdigest()

    # 1. Direct function verification
    assert verify_alertmanager_hmac(body_bytes, valid_sig, secret) is True
    assert verify_alertmanager_hmac(body_bytes, "invalid-hex-sig", secret) is False
    assert verify_alertmanager_hmac(body_bytes, None, secret) is False

    # 2. FastAPI endpoint test
    settings.ALERTMANAGER_HMAC_SECRET = secret
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        # Invalid signature -> 401
        resp_invalid = await ac.post(
            "/api/v1/webhook/alertmanager",
            content=body_bytes,
            headers={"Content-Type": "application/json", "X-Signature": "bad-sig"},
        )
        assert resp_invalid.status_code == 401

        # Valid signature -> 200
        resp_valid = await ac.post(
            "/api/v1/webhook/alertmanager",
            content=body_bytes,
            headers={"Content-Type": "application/json", "X-Signature": valid_sig},
        )
        assert resp_valid.status_code == 200
        data = resp_valid.json()
        assert data["status"] == "accepted"
        assert len(data["incidents"]) == 1
        assert "order-service" in data["incidents"][0]["target"]


@pytest.mark.asyncio
async def test_durable_worker_lease_and_stale_reaping():
    """Tests worker lease claiming, concurrent locking, renewal, and reaper escalation."""
    in_memory_store.incidents.clear()
    pool = await get_db_pool()
    if pool:
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM incident_events; DELETE FROM incidents;")

    # Create a fresh incident
    res = await record_incident_signal(
        cluster_id="test-cluster",
        namespace="production",
        target_kind="Deployment",
        target_name="inventory-service",
        severity="CRITICAL",
        source="ALERTMANAGER",
        signal_name="HighLatency",
        raw_payload={},
    )
    inc_id = res["incident_id"]

    worker_a = WorkerLeaseManager(worker_id="worker-pod-alpha", lease_duration_sec=30)
    worker_b = WorkerLeaseManager(worker_id="worker-pod-beta", lease_duration_sec=30)

    # Worker A claims the incident
    claimed = await worker_a.claim_next_incident()
    assert claimed is not None
    assert str(claimed["id"]) == inc_id
    assert claimed["lease_owner"] == "worker-pod-alpha"

    # Worker B tries to claim -> must be None (locked by Worker A)
    claimed_b = await worker_b.claim_next_incident()
    assert claimed_b is None

    # Worker A renews lease
    renewed = await worker_a.heartbeat(inc_id)
    assert renewed is True

    # Simulate worker crash and expiration
    if pool:
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE incidents SET lease_expires_at = NOW() - INTERVAL '10 seconds' WHERE id = $1::uuid;",
                claimed["id"],
            )
    else:
        in_memory_store.incidents[inc_id]["lease_expires_at"] = None

    # Worker B can now claim the unleased incident
    claimed_b_after = await worker_b.claim_next_incident()
    assert claimed_b_after is not None
    assert str(claimed_b_after["id"]) == inc_id
    assert claimed_b_after["lease_owner"] == "worker-pod-beta"

    # Test stale lease reaper: simulate exceeding max attempts
    if pool:
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE incidents SET attempt_count = 4, lease_expires_at = NOW() - INTERVAL '10 seconds' WHERE id = $1::uuid;",
                claimed["id"],
            )
    else:
        in_memory_store.incidents[inc_id]["attempt_count"] = 4
        from datetime import datetime, timezone, timedelta
        in_memory_store.incidents[inc_id]["lease_expires_at"] = datetime.now(timezone.utc) - timedelta(seconds=10)

    reaped = await worker_a.reap_stale(max_attempts=3)
    assert reaped >= 1
    if pool:
        async with pool.acquire() as conn:
            state = await conn.fetchval("SELECT state FROM incidents WHERE id = $1::uuid;", claimed["id"])
            assert state == "FAILED_ESCALATED"
    else:
        assert in_memory_store.incidents[inc_id]["state"] == "FAILED_ESCALATED"
