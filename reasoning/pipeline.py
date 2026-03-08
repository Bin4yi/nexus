"""
reasoning/pipeline.py
Map-Reduce pipeline orchestrator — enforces the 8,000-token hard limit
at every step. Now accepts primary_targets for grounded reduce output.
"""
from __future__ import annotations
import logging

from openai import OpenAI

from config.settings import settings
from reasoning.map_step import MapStep, MapResult
from reasoning.reduce_step import ReduceStep, NO_COMMUNITIES_MESSAGE

logger = logging.getLogger(__name__)


class MapReducePipeline:
    """
    Orchestrates the full Map-Reduce reasoning flow:
        PR diff text → Map (score communities) → Reduce (synthesize review)

    Token hard limit: 8,000 tokens at every LLM call (spec requirement).
    Reduce step receives grounded FQNs for concrete, file-specific output.
    """

    def __init__(
        self,
        chroma_client,
        llm_client=None,
        n_candidates: int = 20,
        max_workers: int | None = None,
    ):
        from openai import OpenAI
        llm = llm_client or settings.make_llm_client()
        _workers = max_workers or settings.summarizer_max_workers
        self.map_step    = MapStep(chroma_client, llm, n_candidates, _workers)
        self.reduce_step = ReduceStep(llm)

    def analyze_pr(
        self,
        pr_summary: str,
        primary_targets: list[dict] | None = None,
    ) -> dict:
        """
        Full Map-Reduce analysis for a PR.

        Args:
            pr_summary:      Text description of the PR (changed files, methods, etc.)
            primary_targets: List of {fqn, file_path, text} from ChromaDB semantic search.
                             Injected into reduce prompt for grounded, specific output.

        Returns:
            Dict with:
                review (str):              Final architectural review text
                communities_scored (int):  Total communities evaluated in Map step
                communities_used (int):    Non-zero communities passed to Reduce
                map_results (list):        Raw MapResult list
        """
        logger.info("Starting Map-Reduce PR analysis...")

        # MAP STEP (parallel)
        map_results: list[MapResult] = self.map_step.run(pr_summary, mode="pr")
        communities_used = len(map_results)

        # REDUCE STEP — inject real FQNs for grounded output
        if not map_results:
            return {
                "review":               NO_COMMUNITIES_MESSAGE,
                "communities_scored":   0,
                "communities_used":     0,
                "map_results":          [],
            }

        review = self.reduce_step.run(
            map_results,
            query=pr_summary,
            primary_targets=primary_targets,
        )

        return {
            "review":               review,
            "communities_scored":   len(map_results),
            "communities_used":     communities_used,
            "map_results":          map_results,
        }
