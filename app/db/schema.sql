-- KubeSentinel Production Schema DDL (PostgreSQL 16)
-- Enforces Invariants I1-I7, single-active-target atomic deduplication, and durable worker leasing.

CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS "vector";

-- 1. Incidents Table
CREATE TABLE IF NOT EXISTS incidents (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    cluster_id          VARCHAR(64) NOT NULL DEFAULT 'default-cluster',
    namespace           VARCHAR(63) NOT NULL,
    target_kind         VARCHAR(32) NOT NULL CHECK (target_kind IN ('Deployment', 'StatefulSet', 'DaemonSet', 'CronJob', 'Job', 'Pod', 'Rollout')),
    target_name         VARCHAR(253) NOT NULL,
    state               VARCHAR(32) NOT NULL DEFAULT 'INGESTED' CHECK (state IN (
                            'INGESTED', 'INVESTIGATING', 'AWAITING_APPROVAL', 'EXECUTING', 
                            'VERIFYING', 'REVERTING', 'RESOLVED', 'FAILED_ESCALATED', 'ABORTED'
                        )),
    severity            VARCHAR(16) NOT NULL DEFAULT 'WARNING' CHECK (severity IN ('CRITICAL', 'WARNING', 'INFO')),
    signal_count        INT NOT NULL DEFAULT 1,
    hop_count           INT NOT NULL DEFAULT 0 CHECK (hop_count <= 6),
    token_used          INT NOT NULL DEFAULT 0,
    version             BIGINT NOT NULL DEFAULT 1,
    lease_owner         VARCHAR(128),
    lease_expires_at    TIMESTAMPTZ,
    attempt_count       INT NOT NULL DEFAULT 0,
    rca_summary         TEXT,
    rca_confidence      NUMERIC(3,2),
    rca_payload         JSONB,
    first_seen_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    resolved_at         TIMESTAMPTZ
);

-- INVARIANT I1: At most ONE active incident per canonical workload target.
CREATE UNIQUE INDEX IF NOT EXISTS idx_incidents_single_active_target
    ON incidents (cluster_id, namespace, target_kind, target_name)
    WHERE state NOT IN ('RESOLVED', 'FAILED_ESCALATED', 'ABORTED');

-- Worker queue index for FOR UPDATE SKIP LOCKED
CREATE INDEX IF NOT EXISTS idx_incidents_worker_queue
    ON incidents (state, lease_expires_at, severity, first_seen_at)
    WHERE state IN ('INGESTED', 'INVESTIGATING', 'EXECUTING', 'VERIFYING', 'REVERTING');

-- 2. Incident Events Timeline
CREATE TABLE IF NOT EXISTS incident_events (
    id                  BIGSERIAL PRIMARY KEY,
    incident_id         UUID NOT NULL REFERENCES incidents(id) ON DELETE CASCADE,
    source              VARCHAR(32) NOT NULL CHECK (source IN ('ALERTMANAGER', 'K8S_WATCH', 'MANUAL_CLI', 'PROBE')),
    signal_name         VARCHAR(128) NOT NULL,
    raw_payload         JSONB NOT NULL,
    observed_at         TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_incident_events_incident ON incident_events(incident_id, observed_at DESC);

-- 3. Investigation Steps (Durable ReAct Journal)
CREATE TABLE IF NOT EXISTS investigation_steps (
    id                  BIGSERIAL PRIMARY KEY,
    incident_id         UUID NOT NULL REFERENCES incidents(id) ON DELETE CASCADE,
    hop_number          INT NOT NULL CHECK (hop_number BETWEEN 1 AND 6),
    tool_name           VARCHAR(64) NOT NULL,
    tool_args           JSONB NOT NULL,
    thought_rationale   TEXT NOT NULL,
    observation_digest  TEXT NOT NULL,
    raw_observation     JSONB NOT NULL,
    execution_ms        INT NOT NULL,
    is_error            BOOLEAN NOT NULL DEFAULT FALSE,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (incident_id, hop_number, tool_name)
);
CREATE INDEX IF NOT EXISTS idx_steps_incident ON investigation_steps(incident_id, hop_number ASC);

-- 4. Remediation Actions
CREATE TABLE IF NOT EXISTS remediation_actions (
    id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    incident_id             UUID NOT NULL UNIQUE REFERENCES incidents(id) ON DELETE CASCADE,
    mode                    VARCHAR(32) NOT NULL CHECK (mode IN ('OBSERVE_ONLY', 'CLUSTER_DIRECT', 'GITOPS_PR', 'GITOPS_BREAK_GLASS')),
    primitive               VARCHAR(64) NOT NULL CHECK (primitive IN (
                                'ROLLBACK_DEPLOYMENT_REVISION', 'SCALE_WORKLOAD_REPLICAS', 
                                'RESTART_WORKLOAD_ROLLOUT', 'OPEN_GITOPS_PULL_REQUEST', 'NONE_MANUAL_INTERVENTION'
                            )),
    target_namespace        VARCHAR(63) NOT NULL,
    target_kind             VARCHAR(32) NOT NULL,
    target_name             VARCHAR(253) NOT NULL,
    action_params           JSONB NOT NULL,
    expected_resource_ver   VARCHAR(64) NOT NULL,
    pre_state_snapshot      JSONB NOT NULL,
    verification_plan       JSONB NOT NULL,
    approval_hmac_hash      VARCHAR(64) NOT NULL,
    approved_by             VARCHAR(128),
    approved_at             TIMESTAMPTZ,
    executed_at             TIMESTAMPTZ,
    verification_result     JSONB,
    gitops_pr_url           TEXT,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- 5. Two-Tier Knowledge Documents (Runbooks + Quarantined Post-Mortems)
CREATE TABLE IF NOT EXISTS knowledge_documents (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    trust_tier          VARCHAR(32) NOT NULL CHECK (trust_tier IN ('AUTHORITATIVE_RUNBOOK', 'HUMAN_VALIDATED_INCIDENT', 'UNVERIFIED_AUTO_INCIDENT')),
    source_ref          VARCHAR(256) NOT NULL,
    title               TEXT NOT NULL,
    symptom_signature   TEXT NOT NULL,
    content_markdown    TEXT NOT NULL,
    embedding           vector(384),
    search_tsv          tsvector GENERATED ALWAYS AS (
                            setweight(to_tsvector('english', coalesce(title, '')), 'A') ||
                            setweight(to_tsvector('english', coalesce(symptom_signature, '')), 'A') ||
                            setweight(to_tsvector('english', coalesce(content_markdown, '')), 'B')
                        ) STORED,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_knowledge_hnsw ON knowledge_documents USING hnsw (embedding vector_cosine_ops);
CREATE INDEX IF NOT EXISTS idx_knowledge_tsv  ON knowledge_documents USING gin (search_tsv);
