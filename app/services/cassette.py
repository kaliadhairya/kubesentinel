import json
import os
import logging
from typing import Dict, Any, List, Optional, Union
from app.core.config import settings

logger = logging.getLogger("kubesentinel.cassette")


class CassetteReplayEngine:
    """
    Deterministic Cassette Record & Replay Engine.
    Allows running the full ReAct agent loop in CI/CD and local testing with zero LLM API costs.
    """

    def __init__(self, cassettes_dir: Optional[str] = None):
        self.cassettes_dir = cassettes_dir or settings.CASSETTES_DIR
        os.makedirs(self.cassettes_dir, exist_ok=True)

    def load_cassette(self, scenario_name: str) -> Optional[Dict[str, Any]]:
        """Loads and normalizes a recorded cassette fixture by scenario name."""
        clean_name = scenario_name.replace(".json", "")
        file_path = os.path.join(self.cassettes_dir, f"{clean_name}.json")
        if not os.path.exists(file_path):
            logger.warning(f"Cassette file not found: {file_path}")
            return None
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, list):
                    return {
                        "scenario": clean_name,
                        "hops": data,
                        "synthesis": None,
                        "remediation": None,
                    }
                elif isinstance(data, dict):
                    if "hops" not in data:
                        data["hops"] = []
                    return data
                return None
        except Exception as e:
            logger.error(f"Error reading cassette {file_path}: {e}")
            return None

    def find_matching_cassette(
        self,
        query: str,
        namespace: str = "production",
        target_name: str = "checkout-service",
    ) -> Optional[Dict[str, Any]]:
        """Matches an incoming investigation query/workload to a recorded cassette."""
        q = (query + " " + target_name).lower()
        if "checkout" in q or "oom" in q or "137" in q:
            return self.load_cassette("checkout_oom_crash")

        # Check if there's an exact file match for target_name
        direct = self.load_cassette(target_name)
        if direct:
            return direct

        # Fallback to checkout_oom_crash if available as golden fixture
        return self.load_cassette("checkout_oom_crash")

    def record_cassette(
        self,
        scenario_name: str,
        hops: List[Dict[str, Any]],
        synthesis: Optional[Dict[str, Any]] = None,
        remediation: Optional[Dict[str, Any]] = None,
    ):
        """Saves a complete sequence of ReAct hops, synthesis, and remediation."""
        clean_name = scenario_name.replace(".json", "")
        file_path = os.path.join(self.cassettes_dir, f"{clean_name}.json")
        payload = {
            "scenario": clean_name,
            "hops": hops,
            "synthesis": synthesis,
            "remediation": remediation,
        }
        try:
            with open(file_path, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2)
            logger.info(f"Recorded cassette saved to: {file_path}")
        except Exception as e:
            logger.error(f"Error saving cassette {file_path}: {e}")


cassette_engine = CassetteReplayEngine()
