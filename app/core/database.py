import json
import logging
import os
import uuid
import asyncio
from datetime import datetime, timezone
from typing import Optional, Dict, Any, List, Tuple
import asyncpg
from app.core.config import settings

logger = logging.getLogger("kubesentinel.database")

_pool: Optional[asyncpg.Pool] = None

SCHEMA_SQL_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "db", "schema.sql"
)


class InMemoryStateStore:
    """Fallback in-memory store upholding Milestone 1 invariants for offline test suites."""

    def __init__(self):
        self.incidents: Dict[str, Dict[str, Any]] = {}
        self.incident_events: List[Dict[str, Any]] = []
        self.investigation_steps: List[Dict[str, Any]] = []
        self.remediation_actions: Dict[str, Dict[str, Any]] = {}
        self.knowledge_documents: List[Dict[str, Any]] = []

    def record_investigation_step(
        self,
        incident_id: str,
        hop_number: int,
        tool_name: str,
        tool_args: Dict[str, Any],
        thought_rationale: str,
        observation_digest: str,
        raw_observation: Dict[str, Any],
        execution_ms: int,
        is_error: bool = False,
    ) -> Dict[str, Any]:
        step = {
            "id": len(self.investigation_steps) + 1,
            "incident_id": incident_id,
            "hop_number": hop_number,
            "tool_name": tool_name,
            "tool_args": tool_args,
            "thought_rationale": thought_rationale,
            "observation_digest": observation_digest,
            "raw_observation": raw_observation,
            "execution_ms": execution_ms,
            "is_error": is_error,
            "created_at": datetime.now(timezone.utc),
        }
        self.investigation_steps.append(step)
        if incident_id in self.incidents:
            self.incidents[incident_id]["hop_count"] = max(
                self.incidents[incident_id]["hop_count"], hop_number
            )
        return step

    def get_investigation_steps(self, incident_id: str) -> List[Dict[str, Any]]:
        return [
            s for s in self.investigation_steps
            if str(s["incident_id"]) == str(incident_id)
        ]

    def update_incident_rca(
        self,
        incident_id: str,
        rca_summary: str,
        rca_confidence: float,
        rca_payload: Dict[str, Any],
        state: str = "AWAITING_APPROVAL",
    ) -> bool:
        if incident_id in self.incidents:
            inc = self.incidents[incident_id]
            inc["rca_summary"] = rca_summary
            inc["rca_confidence"] = rca_confidence
            inc["rca_payload"] = rca_payload
            inc["state"] = state
            return True
        return False

    def record_remediation_action(
        self,
        incident_id: str,
        mode: str,
        primitive: str,
        target_namespace: str,
        target_kind: str,
        target_name: str,
        action_params: Dict[str, Any],
        expected_resource_ver: str,
        pre_state_snapshot: Dict[str, Any],
        verification_plan: Dict[str, Any],
        approval_hmac_hash: str,
        approved_by: Optional[str] = None,
        gitops_pr_url: Optional[str] = None,
    ) -> Dict[str, Any]:
        action_id = str(uuid.uuid4())
        action = {
            "id": action_id,
            "incident_id": incident_id,
            "mode": mode,
            "primitive": primitive,
            "target_namespace": target_namespace,
            "target_kind": target_kind,
            "target_name": target_name,
            "action_params": action_params,
            "expected_resource_ver": expected_resource_ver,
            "pre_state_snapshot": pre_state_snapshot,
            "verification_plan": verification_plan,
            "approval_hmac_hash": approval_hmac_hash,
            "approved_by": approved_by,
            "approved_at": datetime.now(timezone.utc) if approved_by else None,
            "executed_at": None,
            "verification_result": None,
            "gitops_pr_url": gitops_pr_url,
            "created_at": datetime.now(timezone.utc),
        }
        self.remediation_actions[incident_id] = action
        return action

    def get_remediation_action(self, incident_id: str) -> Optional[Dict[str, Any]]:
        return self.remediation_actions.get(incident_id)

    def set_incident_state(self, incident_id: str, state: str) -> bool:
        if incident_id in self.incidents:
            self.incidents[incident_id]["state"] = state
            if state == "RESOLVED":
                self.incidents[incident_id]["resolved_at"] = datetime.now(timezone.utc)
            return True
        return False

    def record_incident_signal(
        self,
        cluster_id: str,
        namespace: str,
        target_kind: str,
        target_name: str,
        severity: str,
        source: str,
        signal_name: str,
        raw_payload: Dict[str, Any],
    ) -> Tuple[str, bool, int]:
        target_key = (cluster_id, namespace, target_kind, target_name)
        now = datetime.now(timezone.utc)

        # Check existing active incident (INVARIANT I1)
        for inc_id, inc in self.incidents.items():
            if (
                inc["cluster_id"] == cluster_id
                and inc["namespace"] == namespace
                and inc["target_kind"] == target_kind
                and inc["target_name"] == target_name
                and inc["state"] not in ("RESOLVED", "FAILED_ESCALATED", "ABORTED")
            ):
                inc["signal_count"] += 1
                inc["last_seen_at"] = now
                if severity == "CRITICAL":
                    inc["severity"] = "CRITICAL"
                self.incident_events.append({
                    "id": len(self.incident_events) + 1,
                    "incident_id": inc_id,
                    "source": source,
                    "signal_name": signal_name,
                    "raw_payload": raw_payload,
                    "observed_at": now,
                })
                return inc_id, False, inc["signal_count"]

        # Insert new incident
        inc_id = str(uuid.uuid4())
        self.incidents[inc_id] = {
            "id": inc_id,
            "cluster_id": cluster_id,
            "namespace": namespace,
            "target_kind": target_kind,
            "target_name": target_name,
            "state": "INGESTED",
            "severity": severity,
            "signal_count": 1,
            "hop_count": 0,
            "token_used": 0,
            "version": 1,
            "lease_owner": None,
            "lease_expires_at": None,
            "attempt_count": 0,
            "first_seen_at": now,
            "last_seen_at": now,
            "resolved_at": None,
        }
        self.incident_events.append({
            "id": len(self.incident_events) + 1,
            "incident_id": inc_id,
            "source": source,
            "signal_name": signal_name,
            "raw_payload": raw_payload,
            "observed_at": now,
        })
        return inc_id, True, 1


in_memory_store = InMemoryStateStore()


async def get_db_pool() -> Optional[asyncpg.Pool]:
    """Retrieves or initializes the asyncpg connection pool."""
    global _pool
    try:
        current_loop = asyncio.get_running_loop()
    except RuntimeError:
        current_loop = None

    if _pool is not None:
        # Check if pool's event loop matches current running loop
        if current_loop is not None and getattr(_pool, "_loop", None) is current_loop:
            if not getattr(_pool, "_closing", False) and not getattr(_pool, "_closed", False):
                return _pool
        _pool = None

    try:
        dsn = settings.DATABASE_URL.replace("postgresql+asyncpg://", "postgresql://")
        _pool = await asyncpg.create_pool(dsn=dsn, min_size=1, max_size=10, timeout=5.0)
        logger.info("Successfully connected to PostgreSQL connection pool.")
        await init_tables(_pool)
        return _pool
    except Exception as e:
        logger.warning(f"Could not connect to PostgreSQL ({e}). Operating with in-memory state engine.")
        return None


async def init_tables(pool: asyncpg.Pool):
    """Initializes tables for incidents, remediation audits, and memories."""
    async with pool.acquire() as conn:
        try:
            if os.path.exists(SCHEMA_SQL_PATH):
                with open(SCHEMA_SQL_PATH, "r", encoding="utf-8") as f:
                    schema_sql = f.read()
                await conn.execute(schema_sql)
                logger.info("KubeSentinel PostgreSQL 16 schema initialized successfully.")
        except Exception as e:
            logger.error(f"Error applying schema DDL: {e}")
            raise


async def record_incident_signal(
    cluster_id: str,
    namespace: str,
    target_kind: str,
    target_name: str,
    severity: str,
    source: str,
    signal_name: str,
    raw_payload: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Atomic Ingress Upsert adhering to Invariant I1:
    Collapses multiple crash/alert signals for the same active workload into 1 incident row.
    """
    pool = await get_db_pool()
    if pool is None:
        inc_id, is_new, signal_count = in_memory_store.record_incident_signal(
            cluster_id, namespace, target_kind, target_name, severity, source, signal_name, raw_payload
        )
        return {
            "incident_id": inc_id,
            "is_new": is_new,
            "signal_count": signal_count,
            "state": in_memory_store.incidents[inc_id]["state"],
        }

    async with pool.acquire() as conn:
        async with conn.transaction():
            # Invariant I1: Partial Unique Index Upsert
            query = """
            INSERT INTO incidents (cluster_id, namespace, target_kind, target_name, severity, state)
            VALUES ($1, $2, $3, $4, $5, 'INGESTED')
            ON CONFLICT (cluster_id, namespace, target_kind, target_name)
            WHERE state NOT IN ('RESOLVED', 'FAILED_ESCALATED', 'ABORTED')
            DO UPDATE SET
                signal_count = incidents.signal_count + 1,
                last_seen_at = NOW(),
                severity = CASE
                    WHEN EXCLUDED.severity = 'CRITICAL' THEN 'CRITICAL'
                    ELSE incidents.severity
                END
            RETURNING id, state, signal_count, (xmax = 0) AS is_new_incident;
            """
            row = await conn.fetchrow(
                query, cluster_id, namespace, target_kind, target_name, severity
            )
            incident_id = str(row["id"])
            is_new = row["is_new_incident"]
            signal_count = row["signal_count"]
            state = row["state"]

            # Append to timeline
            await conn.execute(
                """
                INSERT INTO incident_events (incident_id, source, signal_name, raw_payload)
                VALUES ($1::uuid, $2, $3, $4::jsonb);
                """,
                incident_id,
                source,
                signal_name,
                json.dumps(raw_payload, default=str),
            )

            return {
                "incident_id": incident_id,
                "is_new": is_new,
                "signal_count": signal_count,
                "state": state,
            }


async def claim_incident_lease(
    worker_id: str,
    lease_duration_sec: int = 30,
) -> Optional[Dict[str, Any]]:
    """
    Claims an active incident using PostgreSQL FOR UPDATE SKIP LOCKED with lease heartbeats.
    """
    pool = await get_db_pool()
    if pool is None:
        # In-memory lease simulation
        now = datetime.now(timezone.utc)
        for inc_id, inc in in_memory_store.incidents.items():
            if inc["state"] in ('INGESTED', 'INVESTIGATING', 'EXECUTING', 'VERIFYING', 'REVERTING'):
                if inc["lease_expires_at"] is None or inc["lease_expires_at"] < now:
                    inc["lease_owner"] = worker_id
                    inc["lease_expires_at"] = datetime.fromtimestamp(
                        now.timestamp() + lease_duration_sec, timezone.utc
                    )
                    if inc["state"] == "INGESTED":
                        inc["state"] = "INVESTIGATING"
                    inc["attempt_count"] += 1
                    inc["version"] += 1
                    return inc
        return None

    query = """
    UPDATE incidents
    SET lease_owner = $1,
        lease_expires_at = NOW() + make_interval(secs => $2),
        attempt_count = attempt_count + 1,
        state = CASE WHEN state = 'INGESTED' THEN 'INVESTIGATING' ELSE state END,
        version = version + 1
    WHERE id = (
        SELECT id
        FROM incidents
        WHERE state IN ('INGESTED', 'INVESTIGATING', 'EXECUTING', 'VERIFYING', 'REVERTING')
          AND (lease_expires_at IS NULL OR lease_expires_at < NOW())
        ORDER BY severity = 'CRITICAL' DESC, first_seen_at ASC
        LIMIT 1
        FOR UPDATE SKIP LOCKED
    )
    RETURNING id, cluster_id, namespace, target_kind, target_name, state, severity, signal_count, hop_count, version, lease_owner, lease_expires_at;
    """
    async with pool.acquire() as conn:
        row = await conn.fetchrow(query, worker_id, lease_duration_sec)
        return dict(row) if row else None


async def renew_incident_lease(
    incident_id: str,
    worker_id: str,
    extension_sec: int = 30,
) -> bool:
    """Renews a heartbeat lease on an active investigation."""
    pool = await get_db_pool()
    if pool is None:
        inc = in_memory_store.incidents.get(incident_id)
        if inc and inc.get("lease_owner") == worker_id:
            now = datetime.now(timezone.utc)
            inc["lease_expires_at"] = datetime.fromtimestamp(
                now.timestamp() + extension_sec, timezone.utc
            )
            return True
        return False

    query = """
    UPDATE incidents
    SET lease_expires_at = NOW() + make_interval(secs => $3)
    WHERE id = $1::uuid AND lease_owner = $2;
    """
    async with pool.acquire() as conn:
        res = await conn.execute(query, incident_id, worker_id, extension_sec)
        return "UPDATE 1" in res


async def reap_stale_leases(max_attempts: int = 3) -> int:
    """
    Stale lease reaper: Fails and escalates incidents that have exceeded max attempts
    to prevent infinite retry loops on poison workloads.
    """
    pool = await get_db_pool()
    if pool is None:
        reaped = 0
        now = datetime.now(timezone.utc)
        for inc_id, inc in in_memory_store.incidents.items():
            if inc["state"] in ('INGESTED', 'INVESTIGATING', 'EXECUTING', 'VERIFYING', 'REVERTING'):
                if inc["lease_expires_at"] and inc["lease_expires_at"] < now:
                    if inc["attempt_count"] >= max_attempts:
                        inc["state"] = "FAILED_ESCALATED"
                        inc["lease_owner"] = None
                        reaped += 1
        return reaped

    query = """
    UPDATE incidents
    SET state = 'FAILED_ESCALATED',
        lease_owner = NULL,
        rca_summary = 'Incident escalation: maximum lease attempts exceeded due to worker failure or timeout.'
    WHERE state IN ('INGESTED', 'INVESTIGATING', 'EXECUTING', 'VERIFYING', 'REVERTING')
      AND lease_expires_at < NOW()
      AND attempt_count >= $1;
    """
    async with pool.acquire() as conn:
        res = await conn.execute(query, max_attempts)
        try:
            return int(res.split(" ")[1])
        except Exception:
            return 0


async def record_investigation_step(
    incident_id: str,
    hop_number: int,
    tool_name: str,
    tool_args: Dict[str, Any],
    thought_rationale: str,
    observation_digest: str,
    raw_observation: Dict[str, Any],
    execution_ms: int,
    is_error: bool = False,
) -> Dict[str, Any]:
    """Persists a ReAct step into PostgreSQL investigation_steps journal."""
    pool = await get_db_pool()
    if pool is None:
        return in_memory_store.record_investigation_step(
            incident_id, hop_number, tool_name, tool_args,
            thought_rationale, observation_digest, raw_observation, execution_ms, is_error
        )

    query = """
    INSERT INTO investigation_steps (
        incident_id, hop_number, tool_name, tool_args,
        thought_rationale, observation_digest, raw_observation,
        execution_ms, is_error
    )
    VALUES ($1::uuid, $2, $3, $4::jsonb, $5, $6, $7::jsonb, $8, $9)
    ON CONFLICT (incident_id, hop_number, tool_name)
    DO UPDATE SET
        thought_rationale = EXCLUDED.thought_rationale,
        observation_digest = EXCLUDED.observation_digest,
        raw_observation = EXCLUDED.raw_observation,
        execution_ms = EXCLUDED.execution_ms,
        is_error = EXCLUDED.is_error
    RETURNING id, incident_id, hop_number, tool_name, created_at;
    """
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                query,
                incident_id,
                hop_number,
                tool_name,
                json.dumps(tool_args, default=str),
                thought_rationale,
                observation_digest,
                json.dumps(raw_observation, default=str),
                execution_ms,
                is_error,
            )
            # Update incident hop_count
            await conn.execute(
                """
                UPDATE incidents
                SET hop_count = GREATEST(hop_count, $2)
                WHERE id = $1::uuid;
                """,
                incident_id,
                hop_number,
            )
            return dict(row)


async def get_investigation_steps(incident_id: str) -> List[Dict[str, Any]]:
    """Retrieves all investigation steps recorded for an incident."""
    pool = await get_db_pool()
    if pool is None:
        return in_memory_store.get_investigation_steps(incident_id)

    query = """
    SELECT id, incident_id, hop_number, tool_name, tool_args,
           thought_rationale, observation_digest, raw_observation,
           execution_ms, is_error, created_at
    FROM investigation_steps
    WHERE incident_id = $1::uuid
    ORDER BY hop_number ASC, id ASC;
    """
    async with pool.acquire() as conn:
        rows = await conn.fetch(query, incident_id)
        return [dict(r) for r in rows]


async def update_incident_rca(
    incident_id: str,
    rca_summary: str,
    rca_confidence: float,
    rca_payload: Dict[str, Any],
    state: str = "AWAITING_APPROVAL",
) -> bool:
    """Updates incident with synthesized RCA and sets state."""
    pool = await get_db_pool()
    if pool is None:
        return in_memory_store.update_incident_rca(
            incident_id, rca_summary, rca_confidence, rca_payload, state
        )

    query = """
    UPDATE incidents
    SET rca_summary = $2,
        rca_confidence = $3,
        rca_payload = $4::jsonb,
        state = $5
    WHERE id = $1::uuid;
    """
    async with pool.acquire() as conn:
        res = await conn.execute(
            query, incident_id, rca_summary, rca_confidence, json.dumps(rca_payload, default=str), state
        )
        return "UPDATE 1" in res


async def set_incident_state(incident_id: str, state: str) -> bool:
    """Updates the state of an incident."""
    pool = await get_db_pool()
    if pool is None:
        return in_memory_store.set_incident_state(incident_id, state)

    query = """
    UPDATE incidents
    SET state = $2::varchar,
        resolved_at = CASE WHEN $2::varchar = 'RESOLVED' THEN NOW() ELSE resolved_at END
    WHERE id = $1::uuid;
    """
    async with pool.acquire() as conn:
        res = await conn.execute(query, incident_id, state)
        return "UPDATE 1" in res


async def record_remediation_action(
    incident_id: str,
    mode: str,
    primitive: str,
    target_namespace: str,
    target_kind: str,
    target_name: str,
    action_params: Dict[str, Any],
    expected_resource_ver: str,
    pre_state_snapshot: Dict[str, Any],
    verification_plan: Dict[str, Any],
    approval_hmac_hash: str,
    approved_by: Optional[str] = None,
    gitops_pr_url: Optional[str] = None,
) -> Dict[str, Any]:
    """Records an executed or planned remediation action in PostgreSQL."""
    pool = await get_db_pool()
    if pool is None:
        return in_memory_store.record_remediation_action(
            incident_id, mode, primitive, target_namespace, target_kind, target_name,
            action_params, expected_resource_ver, pre_state_snapshot, verification_plan,
            approval_hmac_hash, approved_by, gitops_pr_url
        )

    query = """
    INSERT INTO remediation_actions (
        incident_id, mode, primitive, target_namespace, target_kind, target_name,
        action_params, expected_resource_ver, pre_state_snapshot, verification_plan,
        approval_hmac_hash, approved_by, approved_at, executed_at, gitops_pr_url
    )
    VALUES (
        $1::uuid, $2::varchar, $3::varchar, $4::varchar, $5::varchar, $6::varchar,
        $7::jsonb, $8::varchar, $9::jsonb, $10::jsonb,
        $11::varchar, $12::varchar, CASE WHEN $12::varchar IS NOT NULL THEN NOW() ELSE NULL END, NOW(), $13::text
    )
    ON CONFLICT (incident_id) DO UPDATE SET
        mode = EXCLUDED.mode,
        primitive = EXCLUDED.primitive,
        action_params = EXCLUDED.action_params,
        expected_resource_ver = EXCLUDED.expected_resource_ver,
        pre_state_snapshot = EXCLUDED.pre_state_snapshot,
        verification_plan = EXCLUDED.verification_plan,
        approval_hmac_hash = EXCLUDED.approval_hmac_hash,
        approved_by = EXCLUDED.approved_by,
        approved_at = EXCLUDED.approved_at,
        executed_at = EXCLUDED.executed_at,
        gitops_pr_url = EXCLUDED.gitops_pr_url
    RETURNING id, incident_id, mode, primitive, created_at;
    """
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            query,
            incident_id,
            mode,
            primitive,
            target_namespace,
            target_kind,
            target_name,
            json.dumps(action_params, default=str),
            expected_resource_ver,
            json.dumps(pre_state_snapshot, default=str),
            json.dumps(verification_plan, default=str),
            approval_hmac_hash,
            approved_by,
            gitops_pr_url,
        )
        return dict(row)


async def get_remediation_action(incident_id: str) -> Optional[Dict[str, Any]]:
    """Retrieves remediation action details by incident ID."""
    pool = await get_db_pool()
    if pool is None:
        return in_memory_store.get_remediation_action(incident_id)

    query = """
    SELECT id, incident_id, mode, primitive, target_namespace, target_kind, target_name,
           action_params, expected_resource_ver, pre_state_snapshot, verification_plan,
           approval_hmac_hash, approved_by, approved_at, executed_at, verification_result,
           gitops_pr_url, created_at
    FROM remediation_actions
    WHERE incident_id = $1::uuid;
    """
    async with pool.acquire() as conn:
        row = await conn.fetchrow(query, incident_id)
        if not row:
            return None
        d = dict(row)
        for col in ("action_params", "pre_state_snapshot", "verification_plan", "verification_result"):
            if isinstance(d.get(col), str):
                try:
                    d[col] = json.loads(d[col])
                except Exception:
                    pass
        return d


async def update_remediation_verification(
    incident_id: str,
    verification_result: Dict[str, Any],
) -> bool:
    """Updates the verification result of a remediation action."""
    pool = await get_db_pool()
    if pool is None:
        action = in_memory_store.get_remediation_action(incident_id)
        if action:
            action["verification_result"] = verification_result
            return True
        return False

    query = """
    UPDATE remediation_actions
    SET verification_result = $2::jsonb
    WHERE incident_id = $1::uuid;
    """
    async with pool.acquire() as conn:
        res = await conn.execute(query, incident_id, json.dumps(verification_result, default=str))
        return "UPDATE 1" in res


async def close_db_pool():
    """Closes the database pool on application shutdown."""
    global _pool
    if _pool:
        await _pool.close()
        _pool = None
