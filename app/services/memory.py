import logging
from typing import List, Dict, Any, Optional
from app.core.database import get_db_pool

logger = logging.getLogger("kubesentinel.memory")


class IncidentMemoryService:
    """Manages past incident memory, post-mortems, and resolution retrieval."""

    def __init__(self):
        # Fallback local memory if database is not reachable
        self._local_memories: List[Dict[str, Any]] = [
            {
                "incident_id": "INC-7192",
                "service": "checkout-service",
                "symptom": "OOMKilled exit code 137 in checkout container under load",
                "root_cause": "Unbounded local cache in CartService holding session state without TTL",
                "resolution": "Rolled back to v2.4.1, patched Redis cache adapter, and set JVM heap to 75% limit",
            },
            {
                "incident_id": "INC-6810",
                "service": "payment-gateway",
                "symptom": "HTTP 504 Gateway Timeout and p99 latency spike to 5.2s",
                "root_cause": "Downstream third-party webhook connection pool starvation",
                "resolution": "Scaled deployment replicas from 4 to 10 and enabled circuit breaking",
            },
        ]

    async def search_similar_incidents(self, symptom_query: str) -> List[Dict[str, Any]]:
        """Retrieves past incidents with similar failure patterns."""
        pool = await get_db_pool()
        if pool:
            try:
                async with pool.acquire() as conn:
                    rows = await conn.fetch(
                        """
                        SELECT incident_id, symptom, resolution 
                        FROM incident_memory 
                        ORDER BY id DESC LIMIT 3;
                        """
                    )
                    if rows:
                        return [dict(r) for r in rows]
            except Exception as e:
                logger.warning(f"Error querying incident_memory from DB: {e}")

        # Local fallback search
        clean = symptom_query.lower()
        matches = []
        for mem in self._local_memories:
            if any(term in mem["symptom"].lower() for term in clean.split()):
                matches.append(mem)
        return matches or self._local_memories[:2]

    async def save_incident_resolution(
        self,
        incident_id: str,
        symptom: str,
        resolution: str,
    ):
        """Persists a resolved incident for future reference."""
        pool = await get_db_pool()
        if pool:
            try:
                async with pool.acquire() as conn:
                    await conn.execute(
                        """
                        INSERT INTO incident_memory (incident_id, symptom, resolution)
                        VALUES ($1, $2, $3);
                        """,
                        incident_id,
                        symptom,
                        resolution,
                    )
                    logger.info(f"Persisted resolution for {incident_id} into database.")
                    return
            except Exception as e:
                logger.error(f"Failed to persist incident memory to DB: {e}")

        self._local_memories.append({
            "incident_id": incident_id,
            "symptom": symptom,
            "resolution": resolution,
        })


incident_memory = IncidentMemoryService()
