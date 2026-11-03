import json
import logging
from typing import Optional, Dict, Any, List
import asyncpg
from app.core.config import settings

logger = logging.getLogger("kubesentinel.database")

_pool: Optional[asyncpg.Pool] = None


async def get_db_pool() -> Optional[asyncpg.Pool]:
    """Retrieves or initializes the asyncpg connection pool."""
    global _pool
    if _pool is not None:
        return _pool

    try:
        # Convert standard URL for asyncpg
        dsn = settings.DATABASE_URL.replace("postgresql+asyncpg://", "postgresql://")
        _pool = await asyncpg.create_pool(dsn=dsn, min_size=1, max_size=10, timeout=10.0)
        logger.info("Successfully connected to PostgreSQL connection pool.")
        await init_tables(_pool)
        return _pool
    except Exception as e:
        logger.warning(f"Could not connect to PostgreSQL ({e}). Operating in in-memory state mode.")
        return None


async def init_tables(pool: asyncpg.Pool):
    """Initializes tables for incidents, remediation audits, and memories."""
    async with pool.acquire() as conn:
        try:
            await conn.execute("CREATE EXTENSION IF NOT EXISTS vector;")
            has_vector = True
        except Exception:
            has_vector = False
            logger.info("pgvector extension not present, falling back to JSON embeddings.")

        embedding_col = "embedding vector(384)" if has_vector else "embedding jsonb"

        await conn.execute(f"""
            CREATE TABLE IF NOT EXISTS incidents (
                id VARCHAR(64) PRIMARY KEY,
                title TEXT NOT NULL,
                service TEXT NOT NULL,
                namespace TEXT NOT NULL,
                severity VARCHAR(20) NOT NULL,
                status VARCHAR(20) NOT NULL,
                root_cause TEXT,
                timeline JSONB DEFAULT '[]'::jsonb,
                created_at TIMESTAMPTZ DEFAULT NOW(),
                resolved_at TIMESTAMPTZ
            );

            CREATE TABLE IF NOT EXISTS remediation_audit (
                id VARCHAR(64) PRIMARY KEY,
                incident_id VARCHAR(64),
                command TEXT NOT NULL,
                namespace TEXT NOT NULL,
                approved_by TEXT NOT NULL,
                status VARCHAR(20) NOT NULL,
                output TEXT,
                executed_at TIMESTAMPTZ DEFAULT NOW()
            );

            CREATE TABLE IF NOT EXISTS incident_memory (
                id SERIAL PRIMARY KEY,
                incident_id VARCHAR(64),
                symptom TEXT NOT NULL,
                resolution TEXT NOT NULL,
                {embedding_col},
                created_at TIMESTAMPTZ DEFAULT NOW()
            );
        """)
        logger.info("KubeSentinel database schema initialized.")


async def close_db_pool():
    """Closes the database pool on application shutdown."""
    global _pool
    if _pool:
        await _pool.close()
        _pool = None
