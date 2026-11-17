import re
import logging
from typing import Dict, Any, List, Set, Tuple, Optional

logger = logging.getLogger("kubesentinel.citation_gate")


class CitationGateResult:
    """Represents the outcome of Evidence-Grounded Citation Gate verification."""

    def __init__(
        self,
        passed: bool,
        extracted_citations: List[int],
        valid_citations: List[int],
        hallucinated_citations: List[int],
        rejection_reason: Optional[str] = None,
    ):
        self.passed = passed
        self.extracted_citations = extracted_citations
        self.valid_citations = valid_citations
        self.hallucinated_citations = hallucinated_citations
        self.rejection_reason = rejection_reason

    def to_dict(self) -> Dict[str, Any]:
        return {
            "passed": self.passed,
            "extracted_citations": self.extracted_citations,
            "valid_citations": self.valid_citations,
            "hallucinated_citations": self.hallucinated_citations,
            "rejection_reason": self.rejection_reason,
        }


class EvidenceCitationGate:
    """
    Evidence-Grounded Citation Gate.
    Enforces Invariant: No hallucinated diagnosis or remediation.
    Validates that every claim and proposal is grounded in journaled investigation steps.
    """

    CITATION_PATTERNS = [
        re.compile(r"\[(?:Step|Hop)\s*(\d+)\]", re.IGNORECASE),
        re.compile(r"\b(?:step|hop)[_:\s]+(\d+)\b", re.IGNORECASE),
    ]

    @classmethod
    def extract_citations(cls, text: str) -> List[int]:
        """Extracts all referenced step/hop numbers from text."""
        citations: Set[int] = set()
        for pattern in cls.CITATION_PATTERNS:
            for match in pattern.finditer(text):
                try:
                    citations.add(int(match.group(1)))
                except (ValueError, IndexError):
                    continue
        return sorted(list(citations))

    @classmethod
    def validate(
        cls,
        rca_text: str,
        executed_steps: List[Dict[str, Any]],
        explicit_citations: Optional[List[int]] = None,
    ) -> CitationGateResult:
        """
        Validates that all cited steps in the RCA exist in the executed investigation journal.
        """
        valid_hops: Set[int] = set()
        valid_step_ids: Set[int] = set()

        for step in executed_steps:
            if "hop_number" in step:
                valid_hops.add(int(step["hop_number"]))
            if "hop" in step:
                valid_hops.add(int(step["hop"]))
            if "id" in step and step["id"] is not None:
                try:
                    valid_step_ids.add(int(step["id"]))
                except (ValueError, TypeError):
                    pass

        # Combine text-extracted citations with any structured explicit citations
        text_citations = cls.extract_citations(rca_text)
        all_cited = set(text_citations)
        if explicit_citations:
            all_cited.update(explicit_citations)

        extracted = sorted(list(all_cited))

        # If zero citations were provided in the RCA
        if not extracted:
            return CitationGateResult(
                passed=False,
                extracted_citations=[],
                valid_citations=[],
                hallucinated_citations=[],
                rejection_reason="Citation Gate Rejected: Root Cause Analysis contains zero evidence citations to investigation steps.",
            )

        valid_found: List[int] = []
        hallucinated: List[int] = []

        for citation in extracted:
            if citation in valid_hops or citation in valid_step_ids:
                valid_found.append(citation)
            else:
                hallucinated.append(citation)

        if hallucinated:
            return CitationGateResult(
                passed=False,
                extracted_citations=extracted,
                valid_citations=valid_found,
                hallucinated_citations=hallucinated,
                rejection_reason=(
                    f"Citation Gate Rejected: Detected hallucinated step references {hallucinated}. "
                    f"Available valid steps: {sorted(list(valid_hops))}."
                ),
            )

        return CitationGateResult(
            passed=True,
            extracted_citations=extracted,
            valid_citations=valid_found,
            hallucinated_citations=[],
            rejection_reason=None,
        )


citation_gate = EvidenceCitationGate()
