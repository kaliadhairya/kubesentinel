import os
import glob
import logging
from typing import List, Dict, Any, Optional
from app.core.config import settings
from app.core.database import get_db_pool

logger = logging.getLogger("kubesentinel.rag")

_embedder = None


def get_embedding_model():
    """Lazily loads FastEmbed ONNX TextEmbedding model (384-dim, zero PyTorch)."""
    global _embedder
    if _embedder is not None:
        return _embedder

    try:
        from fastembed import TextEmbedding
        _embedder = TextEmbedding(model_name="BAAI/bge-small-en-v1.5")
        logger.info("FastEmbed ONNX TextEmbedding initialized (model=BAAI/bge-small-en-v1.5, dim=384).")
        return _embedder
    except Exception as e:
        logger.warning(f"FastEmbed could not be loaded ({e}). Using deterministic fallback embedding.")
        return None


def generate_embedding(text: str) -> List[float]:
    """Generates 384-dimensional vector embedding for text."""
    model = get_embedding_model()
    if model:
        try:
            embeddings = list(model.embed([text]))
            return embeddings[0].tolist()
        except Exception as e:
            logger.warning(f"Error computing FastEmbed embedding: {e}")

    # Deterministic 384-dim fallback pseudo-vector for offline tests
    import hashlib
    h = hashlib.sha256(text.encode("utf-8")).digest()
    vals = [((b / 255.0) * 2.0 - 1.0) for b in h]
    # Repeat to fill 384 dimensions
    repeated = (vals * 12)[:384]
    # Normalize length
    norm = sum(x * x for x in repeated) ** 0.5 or 1.0
    return [x / norm for x in repeated]


class HybridRAGService:
    """
    Two-Tier Hybrid RAG Service combining:
    1. PostgreSQL tsvector BM25 lexical match (for exact error codes like ExitCode 137, CrashLoopBackOff)
    2. FastEmbed ONNX 384-dim dense vectors (pgvector cosine similarity)
    3. Fused via Reciprocal Rank Fusion (RRF)
    4. Two-Tier Quarantine: Authoritative Runbooks vs Quarantined Unverified Incidents
    """

    def __init__(self):
        self._local_documents: List[Dict[str, Any]] = []

    async def seed_authoritative_runbooks(self):
        """Seeds markdown operational runbooks from data/runbooks/ into authoritative tier."""
        runbooks_dir = settings.RUNBOOKS_DIR
        if not os.path.exists(runbooks_dir):
            return

        md_files = glob.glob(os.path.join(runbooks_dir, "*.md"))
        pool = await get_db_pool()

        for file_path in md_files:
            try:
                with open(file_path, "r", encoding="utf-8") as f:
                    content = f.read()

                filename = os.path.basename(file_path)
                lines = content.strip().split("\n")
                title = lines[0].replace("#", "").strip() if lines else filename
                symptom = ""
                for line in lines:
                    if "Exit Code" in line or "Symptoms" in line or "Error" in line:
                        symptom += line + " "

                symptom = symptom.strip() or title
                embedding = generate_embedding(f"{title}\n{symptom}\n{content[:500]}")

                doc = {
                    "trust_tier": "AUTHORITATIVE_RUNBOOK",
                    "source_ref": filename,
                    "title": title,
                    "symptom_signature": symptom,
                    "content_markdown": content,
                    "embedding": embedding,
                }
                self._local_documents.append(doc)

                if pool:
                    async with pool.acquire() as conn:
                        await conn.execute(
                            """
                            INSERT INTO knowledge_documents (trust_tier, source_ref, title, symptom_signature, content_markdown, embedding)
                            VALUES ($1, $2, $3, $4, $5, $6::vector)
                            ON CONFLICT DO NOTHING;
                            """,
                            "AUTHORITATIVE_RUNBOOK",
                            filename,
                            title,
                            symptom,
                            content,
                            str(embedding),
                        )
                logger.info(f"Indexed authoritative runbook: {title} ({filename})")
            except Exception as e:
                logger.error(f"Error seeding runbook {file_path}: {e}")

    async def search_hybrid(
        self,
        query: str,
        exact_tokens: Optional[List[str]] = None,
        top_k: int = 3,
    ) -> List[Dict[str, Any]]:
        """
        Executes Reciprocal Rank Fusion (RRF) search over authoritative & human-validated knowledge.
        Strictly excludes UNVERIFIED_AUTO_INCIDENT to prevent RAG poisoning.
        """
        pool = await get_db_pool()
        if not pool:
            # Local fallback matching
            clean = query.lower()
            scored = []
            for doc in self._local_documents:
                score = 0.0
                doc_text = (doc["title"] + " " + doc["symptom_signature"] + " " + doc["content_markdown"]).lower()
                for word in clean.split():
                    if word in doc_text:
                        score += 1.0
                if exact_tokens:
                    for tok in exact_tokens:
                        if tok.lower() in doc_text:
                            score += 5.0
                if score > 0:
                    scored.append((score, doc))
            scored.sort(key=lambda x: x[0], reverse=True)
            return [item[1] for item in scored[:top_k]] or self._local_documents[:top_k]

        embedding = generate_embedding(query)
        embedding_str = str(embedding)

        # Build lexical query from clean words and exact tokens
        tokens = [t.strip() for t in query.split() if t.isalnum() and len(t) > 2]
        if exact_tokens:
            tokens.extend([t.strip() for t in exact_tokens if t.strip()])
        lexical_query = " | ".join(tokens) if tokens else query

        rrf_sql = """
        WITH semantic_search AS (
            SELECT id, RANK() OVER (ORDER BY embedding <=> $1::vector) AS sem_rank
            FROM knowledge_documents
            WHERE trust_tier IN ('AUTHORITATIVE_RUNBOOK', 'HUMAN_VALIDATED_INCIDENT')
              AND embedding IS NOT NULL
            ORDER BY embedding <=> $1::vector
            LIMIT 20
        ),
        lexical_search AS (
            SELECT id, RANK() OVER (ORDER BY ts_rank_cd(search_tsv, plainto_tsquery('english', $2)) DESC) AS lex_rank
            FROM knowledge_documents
            WHERE trust_tier IN ('AUTHORITATIVE_RUNBOOK', 'HUMAN_VALIDATED_INCIDENT')
              AND search_tsv @@ plainto_tsquery('english', $2)
            LIMIT 20
        )
        SELECT k.id, k.trust_tier, k.source_ref, k.title, k.symptom_signature, k.content_markdown,
               COALESCE(1.0 / (60 + s.sem_rank), 0.0) + COALESCE(1.0 / (60 + l.lex_rank), 0.0) AS rrf_score
        FROM semantic_search s
        FULL OUTER JOIN lexical_search l ON s.id = l.id
        JOIN knowledge_documents k ON k.id = COALESCE(s.id, l.id)
        ORDER BY rrf_score DESC
        LIMIT $3;
        """

        try:
            async with pool.acquire() as conn:
                rows = await conn.fetch(rrf_sql, embedding_str, lexical_query, top_k)
                if rows:
                    return [dict(r) for r in rows]
        except Exception as e:
            logger.warning(f"PostgreSQL RRF search failed ({e}). Falling back to local vector search.")

        return self._local_documents[:top_k]


hybrid_rag_service = HybridRAGService()
