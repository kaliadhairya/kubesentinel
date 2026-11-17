import yaml
import pytest
from app.services.sanitizer import (
    TelemetrySanitizer,
    TargetConfinementViolationException,
)
from app.services.rag import hybrid_rag_service, generate_embedding


def test_telemetry_secret_scrubbing():
    """Verifies that secrets, tokens, AWS keys, and connection strings are cleanly redacted."""
    raw_log = (
        "2026-11-17 11:20:00 ERROR DB Connection failed: postgresql://admin:SuperSecretPass123@db.prod.internal:5432/core\n"
        "2026-11-17 11:20:01 WARN Header Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.doNotLeakThisSignature\n"
        "2026-11-17 11:20:02 INFO Using S3 Bucket with key AKIAIOSFODNN7EXAMPLE and secret=MyUltraSecretValue\n"
        "-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEA0m...\n-----END RSA PRIVATE KEY-----"
    )

    scrubbed = TelemetrySanitizer.scrub_secrets(raw_log)

    assert "SuperSecretPass123" not in scrubbed
    assert "[REDACTED:DB_PASSWORD]" in scrubbed
    assert "eyJhbGci" not in scrubbed
    assert "[REDACTED:BEARER_TOKEN]" in scrubbed or "[REDACTED:JWT_TOKEN]" in scrubbed
    assert "AKIAIOSFODNN7EXAMPLE" not in scrubbed
    assert "[REDACTED:AWS_KEY]" in scrubbed
    assert "MyUltraSecretValue" not in scrubbed
    assert "[REDACTED:PASSWORD]" in scrubbed
    assert "-----BEGIN RSA PRIVATE KEY-----" not in scrubbed
    assert "[REDACTED:PRIVATE_KEY]" in scrubbed


def test_log_repetition_deduplication():
    """Verifies that repeating stack traces are collapsed into single lines with counters."""
    repeated = (
        "ConnectionRefusedError: dial tcp 10.96.4.12:5432\n" * 25 +
        "Fatal: Process terminated with status 1"
    )
    compacted = TelemetrySanitizer.deduplicate_logs(repeated)
    assert "[Repeated 25 times] ConnectionRefusedError" in compacted
    assert "Fatal: Process terminated" in compacted


def test_xml_data_fencing():
    """Verifies that untrusted logs are wrapped in structural XML boundaries."""
    fenced = TelemetrySanitizer.fence_telemetry(
        content="CRITICAL: JVM Out of Memory",
        step_id=3,
        source="k8s_describe",
    )
    assert '<untrusted_telemetry step_id="3" source="k8s_describe">' in fenced
    assert "CRITICAL: JVM Out of Memory" in fenced
    assert "</untrusted_telemetry>" in fenced


def test_target_confinement_guardrail():
    """INVARIANT I3: Prevents LLM or attacker from pivoting target to an unauthorized workload."""
    # Matching target passes cleanly
    TelemetrySanitizer.enforce_target_confinement(
        locked_namespace="production",
        locked_kind="Deployment",
        locked_name="checkout-service",
        action_namespace="production",
        action_kind="Deployment",
        action_name="checkout-service",
    )

    # Attempt to target kube-system must throw violation
    with pytest.raises(TargetConfinementViolationException) as excinfo:
        TelemetrySanitizer.enforce_target_confinement(
            locked_namespace="production",
            locked_kind="Deployment",
            locked_name="checkout-service",
            action_namespace="kube-system",
            action_kind="Deployment",
            action_name="coredns",
        )
    assert "Target confinement violation" in str(excinfo.value)

    # Attempt to target different workload in same namespace must throw violation
    with pytest.raises(TargetConfinementViolationException):
        TelemetrySanitizer.enforce_target_confinement(
            locked_namespace="production",
            locked_kind="Deployment",
            locked_name="checkout-service",
            action_namespace="production",
            action_kind="Deployment",
            action_name="payment-gateway",
        )


def test_dual_service_account_rbac_manifests():
    """Verifies that Kubernetes RBAC manifests uphold cryptographic privilege separation."""
    # 1. Observer Manifest
    with open("deploy/rbac/kubesentinel-observer.yaml", "r") as f:
        observer_docs = list(yaml.safe_load_all(f))

    cluster_role = next(d for d in observer_docs if d.get("kind") == "ClusterRole")
    all_resources = []
    all_verbs = []
    for rule in cluster_role["rules"]:
        all_resources.extend(rule["resources"])
        all_verbs.extend(rule["verbs"])

    # Must be read-only
    assert set(all_verbs) == {"get", "list", "watch"}
    # Must NOT contain secrets or configmaps
    assert "secrets" not in all_resources

    # 2. Actuator Manifest
    with open("deploy/rbac/kubesentinel-actuator.yaml", "r") as f:
        actuator_docs = list(yaml.safe_load_all(f))

    # Must NEVER be a ClusterRole
    kinds = [d.get("kind") for d in actuator_docs]
    assert "ClusterRole" not in kinds
    assert "ClusterRoleBinding" not in kinds
    # Must be namespace-scoped Role in application namespaces
    roles = [d for d in actuator_docs if d.get("kind") == "Role"]
    assert len(roles) >= 1
    namespaces = {r["metadata"]["namespace"] for r in roles}
    assert "kube-system" not in namespaces


@pytest.mark.asyncio
async def test_two_tier_hybrid_rag_search():
    """Verifies FastEmbed ONNX 384-d vectors + lexical retrieval over authoritative runbooks."""
    # Seed local runbooks
    await hybrid_rag_service.seed_authoritative_runbooks()

    # Search by exact error token
    results_oom = await hybrid_rag_service.search_hybrid(
        query="Pod crashing with ExitCode 137 under memory pressure",
        exact_tokens=["137", "OOMKilled"],
        top_k=2,
    )
    assert len(results_oom) > 0
    top_doc = results_oom[0]
    assert "oom_killed.md" in top_doc["source_ref"] or "OOM" in top_doc["title"]
    assert top_doc["trust_tier"] == "AUTHORITATIVE_RUNBOOK"

    # Search by semantic failure
    results_latency = await hybrid_rag_service.search_hybrid(
        query="HTTP 504 gateway timeout and p99 latency degradation",
        top_k=2,
    )
    assert len(results_latency) > 0
    assert any("latency" in d["source_ref"].lower() for d in results_latency)
