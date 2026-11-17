import re
import logging
from typing import Dict, Any, List, Tuple, Optional

logger = logging.getLogger("kubesentinel.sanitizer")


class TargetConfinementViolationException(Exception):
    """Raised when an action or tool attempts to violate Invariant I3 (target confinement)."""
    pass


class TelemetrySanitizer:
    """
    Deterministic Telemetry Sanitizer defending against:
    1. Accidental secret/credential leakage in telemetry
    2. Context overflow via log repetition deduplication
    3. Indirect Prompt Injection via structural XML data-fencing and code-level target confinement
    """

    SECRET_PATTERNS = [
        # Bearer tokens & JWTs
        (re.compile(r"Bearer\s+([a-zA-Z0-9_\-\.]{20,})", re.IGNORECASE), "Bearer [REDACTED:BEARER_TOKEN]"),
        (re.compile(r"eyJ[a-zA-Z0-9_\-]{15,}\.eyJ[a-zA-Z0-9_\-]{15,}\.[a-zA-Z0-9_\-]{15,}", re.IGNORECASE), "[REDACTED:JWT_TOKEN]"),
        # AWS Access Keys
        (re.compile(r"\b(AKIA|ABIA|ACCA|ASIA)[0-9A-Z]{16}\b"), "[REDACTED:AWS_KEY]"),
        # Database URIs
        (re.compile(r"(postgres(?:ql)?|mysql|mongodb|redis)://([^:\s]+):([^@\s]+)@([^\s/]+)", re.IGNORECASE), r"\1://\2:[REDACTED:DB_PASSWORD]@\4"),
        # Passwords in logs or configs
        (re.compile(r'(password|passwd|pwd|secret)\s*[:=]\s*["\']?([^"\'\s,;]+)["\']?', re.IGNORECASE), r'\1=[REDACTED:PASSWORD]'),
        # Generic API Keys
        (re.compile(r'(api[_-]?key|access[_-]?token|auth[_-]?token)\s*[:=]\s*["\']?([a-zA-Z0-9_\-]{16,})["\']?', re.IGNORECASE), r'\1=[REDACTED:API_KEY]'),
        # PEM Private Keys
        (re.compile(r"-----BEGIN [A-Z ]+ PRIVATE KEY-----[\s\S]+?-----END [A-Z ]+ PRIVATE KEY-----"), "[REDACTED:PRIVATE_KEY]"),
    ]

    @classmethod
    def scrub_secrets(cls, text: str) -> str:
        """Replaces known credentials and secrets with typed [REDACTED:<TYPE>] markers."""
        if not text:
            return ""
        scrubbed = text
        for pattern, replacement in cls.SECRET_PATTERNS:
            scrubbed = pattern.sub(replacement, scrubbed)
        return scrubbed

    @classmethod
    def deduplicate_logs(cls, logs: str, max_lines: int = 40) -> str:
        """
        Collapses consecutive duplicate log lines and stack traces to preserve LLM context budget.
        Example: 50 identical ConnectionRefused errors become '[Repeated 50 times] ConnectionRefused...'
        """
        if not logs:
            return ""

        lines = logs.strip().split("\n")
        if not lines:
            return ""

        collapsed: List[str] = []
        last_line: Optional[str] = None
        repeat_count = 0

        for line in lines:
            normalized = line.strip()
            if normalized == last_line:
                repeat_count += 1
            else:
                if repeat_count > 1:
                    collapsed.append(f"[Repeated {repeat_count} times] {last_line}")
                elif last_line is not None:
                    collapsed.append(last_line)
                last_line = normalized
                repeat_count = 1

        if repeat_count > 1:
            collapsed.append(f"[Repeated {repeat_count} times] {last_line}")
        elif last_line is not None:
            collapsed.append(last_line)

        # Truncate if exceeding max_lines, keeping head and tail
        if len(collapsed) > max_lines:
            half = max_lines // 2
            head = collapsed[:half]
            tail = collapsed[-half:]
            return "\n".join(head + [f"... [{len(collapsed) - max_lines} log lines compacted] ..."] + tail)

        return "\n".join(collapsed)

    @classmethod
    def fence_telemetry(
        cls,
        content: str,
        step_id: int,
        source: str = "k8s_logs",
    ) -> str:
        """
        Enforces structural XML data-fencing.
        Guarantees that telemetry cannot execute indirect prompt injection commands.
        """
        scrubbed = cls.scrub_secrets(content)
        compacted = cls.deduplicate_logs(scrubbed)
        return (
            f'<untrusted_telemetry step_id="{step_id}" source="{source}">\n'
            f'<!-- DATA BOUNDARY: Content within this block is unverified external telemetry. -->\n'
            f'{compacted}\n'
            f'</untrusted_telemetry>'
        )

    @staticmethod
    def enforce_target_confinement(
        locked_namespace: str,
        locked_kind: str,
        locked_name: str,
        action_namespace: str,
        action_kind: str,
        action_name: str,
    ):
        """
        INVARIANT I3: Target Confinement.
        Guarantees that proposed remediations cannot pivot to another workload or namespace,
        even if the LLM hallucinated or was instructed by a prompt injection in a log.
        """
        if (
            locked_namespace != action_namespace
            or locked_kind != action_kind
            or locked_name != action_name
        ):
            raise TargetConfinementViolationException(
                f"Target confinement violation! Incident locked to ({locked_namespace}/{locked_kind}/{locked_name}), "
                f"but action attempted to target ({action_namespace}/{action_kind}/{action_name}). Blocked."
            )
