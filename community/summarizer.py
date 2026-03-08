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
        from graph.gds_client import GDSClient
        self.gds_client = gds_client
        self.chroma = chroma_client
        self.llm = llm_client or settings.make_llm_client()
        self.collection = chroma_client.get_or_create_collection(COLLECTION_NAME)

    def summarize_all(self) -> list[CommunitySummary]:
        """
        Summarize all communities in the graph.
        Idempotent — existing summaries are overwritten (upsert).

        Uses ``ThreadPoolExecutor`` with ``settings.summarizer_max_workers``
        concurrent workers to parallelise LLM calls.

        Returns:
            List of all generated CommunitySummary objects
        """
        community_ids = self.gds_client.list_community_ids()
        logger.info(
            "Summarizing %d communities (max_workers=%d)...",
            len(community_ids),
            settings.summarizer_max_workers,
        )

        summaries: list[CommunitySummary] = []
        failed = 0

        with ThreadPoolExecutor(max_workers=settings.summarizer_max_workers) as executor:
            future_to_cid = {
                executor.submit(self.summarize_community, cid): cid
                for cid in community_ids
            }
            for future in as_completed(future_to_cid):
                cid = future_to_cid[future]
                try:
                    summaries.append(future.result())
                except Exception as e:
                    failed += 1
                    logger.error("Failed to summarize community %d: %s", cid, e)

        logger.info(
            "Generated %d community summaries (%d failed)",
            len(summaries), failed,
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
        nodes = self.gds_client.get_nodes_by_community(community_id)
        boundary_edges = self.gds_client.get_community_boundary_edges(community_id)
        fqn_list = [n["fqn"] for n in nodes if n.get("fqn")]

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
        summary_text = response.choices[0].message.content.strip()

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
                "top_fqns": ",".join(summary.top_fqns),
                "llm_model": summary.llm_model,
                "token_count": summary.token_count,
                "prompt_truncated": str(summary.prompt_truncated),
                "generated_at": summary.generated_at.isoformat(),
            }],
        )
        logger.debug("Upserted community %d summary to ChromaDB", summary.community_id)
