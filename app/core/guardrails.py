import re
from typing import Tuple, List, Optional
from app.core.config import settings


class GuardrailViolationException(Exception):
    """Raised when an SRE action violates safety policies."""
    pass


class InfraGuardrails:
    """Zero-trust safety layer to prevent catastrophic infrastructure mutations."""

    # Unconditional blocklist: destructive patterns that are NEVER allowed
    UNCONDITIONAL_BLOCKLIST: List[re.Pattern] = [
        re.compile(r"\bdelete\s+(namespace|ns|clusterrole|crd|nodes?)\b", re.IGNORECASE),
        re.compile(r"\bdelete\s+--all\b", re.IGNORECASE),
        re.compile(r"\b(rm\s+-[rf]{1,2}|mkfs|dd\s+if=|fdisk|format)\b", re.IGNORECASE),
        re.compile(r"\bdrop\s+(database|schema|table)\b", re.IGNORECASE),
        re.compile(r"\btruncate\s+(table)?\b", re.IGNORECASE),
        re.compile(r":\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;", re.IGNORECASE),  # fork bomb
    ]

    # Protected system namespaces
    PROTECTED_SYSTEM_NAMESPACES = {"kube-system", "kube-public", "kube-node-lease"}

    # Allowed safe mutating operations (require human-in-the-loop approval)
    ALLOWED_MUTATIONS = {"rollout undo", "rollout restart", "scale"}

    @classmethod
    def validate_command(cls, command: str, namespace: Optional[str] = None) -> Tuple[bool, str]:
        """Validates any proposed command against guardrails.
        
        Returns (is_allowed, reason).
        """
        clean_cmd = command.strip().lower()

        # 1. Check unconditional blocklist
        for pattern in cls.UNCONDITIONAL_BLOCKLIST:
            if pattern.search(clean_cmd):
                raise GuardrailViolationException(
                    f"Blocked command matches forbidden destructive pattern: '{pattern.pattern}'"
                )

        # 2. Check namespace rules
        if namespace:
            target_ns = namespace.strip().lower()
            if target_ns in cls.PROTECTED_SYSTEM_NAMESPACES:
                raise GuardrailViolationException(
                    f"Operation forbidden on protected system namespace: '{namespace}'"
                )

            allowed_ns = settings.allowed_namespaces
            if allowed_ns and target_ns not in allowed_ns:
                raise GuardrailViolationException(
                    f"Namespace '{namespace}' is not in allowed target namespaces: {allowed_ns}"
                )

        # 3. Detect if command is mutating
        is_mutating = any(mutation in clean_cmd for mutation in cls.ALLOWED_MUTATIONS)
        if "delete" in clean_cmd:
            raise GuardrailViolationException(
                f"Delete commands are forbidden. Use rollback, scale down, or drain instead."
            )

        if is_mutating:
            return True, "MUTATING_REQUIRES_APPROVAL"

        return True, "READ_ONLY_SAFE"

    @classmethod
    def sanitize_promql(cls, query: str) -> str:
        """Sanitizes a PromQL query string to prevent injection."""
        sanitized = query.strip()
        # Ensure it does not contain shell escape sequences
        if any(char in sanitized for char in [";", "`", "$", "&&", "||"]):
            raise GuardrailViolationException("PromQL query contains illegal shell characters.")
        return sanitized
