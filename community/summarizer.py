"""
community/summarizer.py
Generates LLM summaries for each Leiden community and stores them in ChromaDB.

Performance:
    Uses ``ThreadPoolExecutor`` with ``settings.summarizer_max_workers``
    concurrent LLM calls so that summarization scales with the number of
    communities (100+ repos → thousands of communities).

All heavy imports (chromadb, openai) are deferred into __init__ to avoid
pydantic-v1 shim crash on Python 3.14 at module collection time.
"""
from __future__ import annotations
import hashlib
import json
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional

from config.settings import settings
from community.models import CommunitySummary
from community.prompt_builder import (
    build_community_prompt,
    count_tokens,
    SYSTEM_PROMPT,
)

logger = logging.getLogger(__name__)

COLLECTION_NAME = "community_summaries"


class CommunitySummarizer:
    """
    For each Leiden community in Neo4j:
    1. Fetch member nodes (FQNs, docstrings, kinds)
    2. Build a budget-capped LLM prompt
    3. Call LLM → get CommunitySummary
    4. Upsert into ChromaDB 'community_summaries' collection

    The ChromaDB GEID bridge: metadata.community_id links back to Neo4j.
    """

    def __init__(
        self,
        gds_client,
        chroma_client,
        llm_client=None,
    ):
        import chromadb
        from openai import OpenAI
        self.gds_client = gds_client
        self.chroma = chroma_client
        self.llm = llm_client or settings.make_llm_client()
        self.collection = chroma_client.get_or_create_collection(COLLECTION_NAME)

    @staticmethod
    def _community_hash(fqn_list: list[str]) -> str:
        """
        Stable SHA-1 hash of the sorted FQN list for a community.
        If two successive ingestion runs produce the same hash, the community
        structure has not changed — skip the LLM call entirely.
        Cost: O(N * len(fqn)) in CPU, zero network/API calls.
        """
        digest = hashlib.sha1(
            "\n".join(sorted(fqn_list)).encode("utf-8")
        ).hexdigest()[:16]
        return digest

    def _load_existing_hashes(self) -> dict[int, str]:
        """
        Return {community_id: fqn_hash} for all summaries already in ChromaDB.
        If the collection is empty or the metadata lacks 'fqn_hash', returns {}.
        """
        existing: dict[int, str] = {}
        try:
            count = self.collection.count()
            if count == 0:
                return existing
            batch = self.collection.get(
                limit=count,
                include=["metadatas"],
            )
            for meta in batch.get("metadatas") or []:
                cid  = meta.get("community_id")
                h    = meta.get("fqn_hash", "")
                if cid is not None and h:
                    existing[int(cid)] = h
        except Exception as e:
            logger.warning("Could not load existing community hashes: %s", e)
        return existing

    def summarize_all(self) -> list[CommunitySummary]:
        """
        Summarize all communities in the graph.

        Incremental — communities whose FQN set is unchanged (same hash) are
        skipped, so re-running after adding one repo only re-summarizes the
        affected communities.  LLM cost is proportional to change, not total
        graph size.

        Uses ``ThreadPoolExecutor`` with ``settings.summarizer_max_workers``
        concurrent workers to parallelise LLM calls.

        Returns:
            List of newly generated CommunitySummary objects (skipped ones omitted).
        """
        community_ids = self.gds_client.list_community_ids()
        existing_hashes = self._load_existing_hashes()

        # Pre-fetch FQN lists to compute hashes (cheap Neo4j reads, no LLM).
        # Cache node lists so summarize_community() can reuse them without
        # a second round-trip to Neo4j.
        to_summarize: list[int] = []
        _node_cache: dict[int, list[dict]] = {}
        skipped = 0
        for cid in community_ids:
            nodes = self.gds_client.get_nodes_by_community(cid)
            fqn_list = [n["fqn"] for n in nodes if n.get("fqn")]
            new_hash = self._community_hash(fqn_list)
            if existing_hashes.get(cid) == new_hash:
                skipped += 1
            else:
                _node_cache[cid] = nodes
                to_summarize.append(cid)

        self._node_cache = _node_cache  # thread-safe read (written before pool starts)

        logger.info(
            "Communities: %d total, %d unchanged (skipping), %d to re-summarize "
            "(max_workers=%d)…",
            len(community_ids), skipped, len(to_summarize),
            settings.summarizer_max_workers,
        )

        summaries: list[CommunitySummary] = []
        failed = 0

        with ThreadPoolExecutor(max_workers=settings.summarizer_max_workers) as executor:
            future_to_cid = {
                executor.submit(self.summarize_community, cid): cid
                for cid in to_summarize
            }
            for future in as_completed(future_to_cid):
                cid = future_to_cid[future]
                try:
                    summaries.append(future.result())
                except Exception as e:
                    failed += 1
                    logger.error("Failed to summarize community %d: %s", cid, e)

        logger.info(
            "Done. New/updated: %d, skipped: %d, failed: %d",
            len(summaries), skipped, failed,
        )
        return summaries

    def summarize_community(self, community_id: int) -> CommunitySummary:
        """
        Generate and store a summary for a single community.

        Args:
            community_id: The Leiden community integer ID

        Returns:
            CommunitySummary with LLM output and token count
        """
        # Use pre-fetched nodes from summarize_all() cache if available,
        # otherwise fetch directly (supports calling summarize_community() standalone).
        cache = getattr(self, "_node_cache", {})
        nodes = cache.get(community_id) or self.gds_client.get_nodes_by_community(community_id)
        boundary_edges = self.gds_client.get_community_boundary_edges(community_id)
        fqn_list = [n["fqn"] for n in nodes if n.get("fqn")]
        fqn_hash = self._community_hash(fqn_list)

        # Build prompt (token-budget-enforced, includes class-level context + boundary edges)
        prompt, truncated = build_community_prompt(
            community_id, nodes, boundary_edges=boundary_edges,
        )
        token_count = count_tokens(SYSTEM_PROMPT + prompt)

        # Call LLM
        response = self.llm.chat.completions.create(
            model=settings.llm_model,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            max_completion_tokens=settings.community_summary_max_tokens,
        )
        content = response.choices[0].message.content or ""
        if not content:
            logger.warning(
                "Empty LLM response for community %d — using empty summary", community_id
            )
        summary_text = content.strip()

        # Select top 5 FQNs (prioritise class-level over method-level)
        class_fqns = [n["fqn"] for n in nodes
                       if n.get("label") == "Component" and n.get("fqn")]
        method_fqns = [n["fqn"] for n in nodes
                        if n.get("label") != "Component" and n.get("fqn")]
        top_fqns = (class_fqns + method_fqns)[:5]

        summary = CommunitySummary(
            community_id=community_id,
            summary_text=summary_text,
            node_count=len(nodes),
            fqn_list=fqn_list,
            top_fqns=top_fqns,
            llm_model=settings.llm_model,
            token_count=token_count,
            prompt_truncated=truncated,
            fqn_hash=fqn_hash,
        )

        # Upsert into ChromaDB
        self._upsert_to_chroma(summary)
        return summary

    # ── Private ───────────────────────────────────────────────────────────────

    def _upsert_to_chroma(self, summary: CommunitySummary) -> None:
        """Upsert community summary into ChromaDB with full metadata."""
        doc_id = f"community_{summary.community_id}"
        self.collection.upsert(
            ids=[doc_id],
            documents=[summary.summary_text],
            metadatas=[{
                "community_id": summary.community_id,
                "node_count": summary.node_count,
                "top_fqns": json.dumps(summary.top_fqns),
                "llm_model": summary.llm_model,
                "token_count": summary.token_count,
                "prompt_truncated": str(summary.prompt_truncated),
                "generated_at": summary.generated_at.isoformat(),
                "fqn_hash": summary.fqn_hash,
            }],
        )
        logger.debug("Upserted community %d summary to ChromaDB", summary.community_id)
