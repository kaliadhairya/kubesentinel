import pytest
from app.core.guardrails import InfraGuardrails, GuardrailViolationException


def test_destructive_commands_blocked():
    """Ensures destructive shell and kubectl commands are strictly blocked."""
    forbidden = [
        "kubectl delete namespace production",
        "kubectl delete ns default",
        "rm -rf /var/log",
        "DROP DATABASE production;",
        "TRUNCATE TABLE users;",
        "kubectl delete clusterrole admin",
        "kubectl delete pod --all",
    ]
    for cmd in forbidden:
        with pytest.raises(GuardrailViolationException):
            InfraGuardrails.validate_command(cmd)


def test_protected_system_namespaces_blocked():
    """Ensures operations on kube-system are blocked."""
    with pytest.raises(GuardrailViolationException):
        InfraGuardrails.validate_command("kubectl get pods", namespace="kube-system")


def test_safe_read_commands_allowed():
    """Ensures safe inspection commands pass with READ_ONLY_SAFE."""
    allowed, status = InfraGuardrails.validate_command("kubectl get pods -n production", namespace="production")
    assert allowed is True
    assert status == "READ_ONLY_SAFE"

    allowed, status = InfraGuardrails.validate_command("kubectl describe pod checkout-1", namespace="production")
    assert allowed is True
    assert status == "READ_ONLY_SAFE"


def test_mutating_rollback_requires_approval():
    """Ensures rollout undo is allowed but flagged as requiring approval."""
    allowed, status = InfraGuardrails.validate_command(
        "kubectl rollout undo deployment/checkout -n production", namespace="production"
    )
    assert allowed is True
    assert status == "MUTATING_REQUIRES_APPROVAL"


def test_promql_sanitization():
    """Ensures PromQL queries reject shell injection attempts."""
    clean = InfraGuardrails.sanitize_promql("rate(http_requests_total[5m])")
    assert clean == "rate(http_requests_total[5m])"

    with pytest.raises(GuardrailViolationException):
        InfraGuardrails.sanitize_promql("rate(http_requests_total[5m]); rm -rf /")
