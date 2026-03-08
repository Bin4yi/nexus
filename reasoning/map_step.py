"""
reasoning/map_step.py
Map step of the GraphRAG Map-Reduce reasoning pipeline.

PARALLELIZED: All community scoring LLM calls run concurrently via
ThreadPoolExecutor, cutting latency from O(N * RTT) to ~O(RTT).

When the router provides deterministic blast-radius community IDs, the Map
step can skip the ChromaDB vector search entirely (``run_deterministic``)
and score only those communities — guaranteeing zero semantic drift.
"""
from __future__ import annotations
import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass

import tiktoken

from config.settings import settings
from community.summarizer import COLLECTION_NAME as COMMUNITY_COLLECTION

logger = logging.getLogger(__name__)

_ENCODER = tiktoken.get_encoding("cl100k_base")

# ── PR mode prompts (blast-radius / impact analysis) ──────────────────────

MAP_SYSTEM_PROMPT_PR = (
    "You are a senior Java architect performing a code blast-radius analysis. "
    "You will be shown a PR summary and a cluster of code. "
    "Your job is to assess whether this code cluster is architecturally "
    "impacted by the PR changes."
)

MAP_USER_TEMPLATE_PR = """
PR changes the following Java code:
{input_text}

This code community (ID={community_id}) contains:
{community_summary}

Is this community architecturally impacted by the PR?
Respond ONLY with valid JSON on a single line:
{{"score": <integer 0-100>, "reason": "<one sentence>"}}

Score guide: 0=no impact, 1-29=minor, 30-69=moderate, 70-100=high impact.
""".strip()

# ── Question mode prompts (user asking a question about the codebase) ─────

MAP_SYSTEM_PROMPT_QUESTION = (
    "You are a senior Java architect assessing code relevance. "
    "You will be shown a developer's question about a codebase and a summary "
    "of a code community (a cluster of related classes/methods). "
    "Your job is to assess how RELEVANT this community is for answering the question."
)

MAP_USER_TEMPLATE_QUESTION = """
Developer question:
{input_text}

This code community (ID={community_id}) contains:
{community_summary}

How relevant is this community for answering the developer's question?
Respond ONLY with valid JSON on a single line:
{{"score": <integer 0-100>, "reason": "<one sentence>"}}

Score guide:
  0       = completely irrelevant
  1-29    = tangentially related (mentions similar concepts but wrong area)
  30-69   = moderately relevant (related subsystem, partial answer)
  70-100  = highly relevant (directly contains code/logic needed to answer)
""".strip()

# Backward-compat aliases
MAP_SYSTEM_PROMPT = MAP_SYSTEM_PROMPT_PR
MAP_USER_TEMPLATE = MAP_USER_TEMPLATE_PR


@dataclass
class MapResult:
    """Scoring result for a single community."""
    community_id: int
    score: int          # 0–100
    reason: str
    summary_text: str   # passed through for the reduce step


class MapStep:
    """
    Queries ChromaDB for relevant communities, then asks the LLM to
    score each community's blast-radius relevance to the PR.

    All LLM scoring calls are made in PARALLEL via ThreadPoolExecutor
    to eliminate the O(N × RTT) latency bottleneck.
    """

    def __init__(
        self,
        chroma_client,
        llm_client=None,
        n_candidates: int = 20,
        max_workers: int = 10,
    ):
        from openai import OpenAI
        self.collection = chroma_client.get_or_create_collection(COMMUNITY_COLLECTION)
        self.llm = llm_client or settings.make_llm_client()
        self.n_candidates = n_candidates
        self.max_workers = max_workers  # parallel LLM calls

    def run(
        self,
        input_text: str,
        restrict_community_ids: list[int] | None = None,
        mode: str = "pr",
    ) -> list[MapResult]:
        """
        Execute the Map step.

        For **question** mode: ChromaDB already ranked candidates by cosine
        similarity — the LLM re-scoring is redundant.  We convert the vector
        distance directly to a 0-100 score (zero LLM calls).

        For **pr** (blast-radius) mode: the LLM scores each community because
        blast-radius analysis requires genuine architectural judgment beyond
        what cosine similarity captures.

        Args:
            input_text:             Text — either a PR description or a user question
            restrict_community_ids: When not *None*, only score these communities
                                    (deterministic blast-radius path).
            mode:                   "pr" for LLM blast-radius scoring,
                                    "question" for fast distance-based scoring.

        Returns:
            List of MapResult, sorted by score descending, zero-scores removed
        """
        if restrict_community_ids is not None:
            candidates = self._retrieve_by_ids(restrict_community_ids)
            # For deterministic IDs there are no distances — use LLM scoring
            # regardless of mode so we get meaningful relevance signals.
            return self._llm_score(input_text, candidates, mode)

        if mode == "question":
            # Fast path: retrieve top-N by vector similarity, score by distance.
            # Zero LLM calls — ChromaDB's cosine distance IS the relevance signal.
            candidates_with_dist = self._retrieve_candidates_with_distances(input_text)
            results = [
                MapResult(
                    community_id=c["community_id"],
                    score=c["score"],
                    reason="ChromaDB similarity score (no LLM call)",
                    summary_text=c["summary_text"],
                )
                for c in candidates_with_dist if c["score"] > 0
            ]
            results.sort(key=lambda r: r.score, reverse=True)
            logger.info(
                "Map step (fast/question): %d candidates → %d non-zero scored communities",
                len(candidates_with_dist), len(results),
            )
            return results

        # PR mode — use LLM scoring
        candidates = self._retrieve_candidates(input_text)
        return self._llm_score(input_text, candidates, mode)

    def _llm_score(
        self,
        input_text: str,
        candidates: list[dict],
        mode: str,
    ) -> list[MapResult]:
        """Score candidates using parallel LLM calls (PR / blast-radius mode)."""
        if not candidates:
            logger.warning("No community candidates found for query")
            return []

        results: list[MapResult] = []
        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            futures = {
                executor.submit(self._score_community, input_text, c, mode): c
                for c in candidates
            }
            for future in as_completed(futures):
                try:
                    results.append(future.result())
                except Exception as e:
                    cand = futures[future]
                    logger.warning(
                        "Scoring failed for community %s: %s",
                        cand.get("community_id"), e
                    )

        filtered = [r for r in results if r.score > 0]
        filtered.sort(key=lambda r: r.score, reverse=True)
        logger.info(
            "Map step (LLM/pr): %d candidates → %d non-zero scored communities",
            len(candidates), len(filtered),
        )
        return filtered

    # ── Private ───────────────────────────────────────────────────────────────

    def _retrieve_candidates_with_distances(self, query: str) -> list[dict]:
        """Semantic search — returns top N candidates with distance converted to score.

        Used by question mode (no LLM scoring).  Capped at ``n_candidates``
        (default 20) — no scaling formula needed since we trust the vector
        distance ranking directly.
        """
        total = self.collection.count()
        n = min(self.n_candidates, total) if total > 0 else self.n_candidates
        try:
            results = self.collection.query(
                query_texts=[query],
                n_results=n,
                include=["documents", "metadatas", "distances"],
            )
        except Exception as e:
            logger.error("ChromaDB query failed: %s", e)
            return []

        candidates = []
        if results["ids"]:
            for i, doc_id in enumerate(results["ids"][0]):
                meta = results["metadatas"][0][i]
                # ChromaDB returns L2 or cosine distance: lower = more similar.
                # Convert to 0-100 score: score = max(0, round((1 - distance) * 100))
                # For cosine distance (0=identical, 2=opposite): score = (1 - d/2) * 100
                dist = results["distances"][0][i]
                if dist > 2:
                    logger.warning(
                        "Distance > 2 detected (likely L2, not cosine): %f for doc %s. "
                        "Ensure ChromaDB collection uses cosine distance.",
                        dist, doc_id,
                    )
                score = max(0, round((1.0 - dist / 2.0) * 100))
                cid = meta.get("community_id")
                if cid is None:
                    try:
                        cid = int(doc_id.split("_")[-1])
                    except (ValueError, IndexError):
                        raise ValueError(f"Invalid community_id format: {doc_id}")
                candidates.append({
                    "community_id": cid,
                    "summary_text": results["documents"][0][i],
                    "score": score,
                })
        return candidates

    def _retrieve_candidates(self, pr_summary: str) -> list[dict]:
        """Semantic search ChromaDB for relevant community summaries (PR mode).

        Uses the scaling formula to ensure large repos get adequate LLM coverage.
        Capped at 50 to keep PR analysis tractable with GPT-5 reasoning costs.
        """
        total = self.collection.count()
        n = min(max(self.n_candidates, total // 4), 50) if total > 0 else self.n_candidates
        try:
            results = self.collection.query(
                query_texts=[pr_summary],
                n_results=min(n, total),
            )
        except Exception as e:
            logger.error("ChromaDB query failed: %s", e)
            return []

        candidates = []
        if results["ids"]:
            for i, doc_id in enumerate(results["ids"][0]):
                meta = results["metadatas"][0][i]
                candidates.append({
                    "community_id": meta.get("community_id", 0),
                    "summary_text": results["documents"][0][i],
                })
        return candidates

    def _retrieve_by_ids(self, community_ids: list[int]) -> list[dict]:
        """
        Fetch community summaries by their deterministic IDs (no vector search).

        Used by the deterministic blast-radius path so that scoring is
        restricted to mathematically-proven affected communities.
        """
        if not community_ids:
            return []
        doc_ids = [f"community_{cid}" for cid in community_ids]
        try:
            results = self.collection.get(ids=doc_ids, include=["documents", "metadatas"])
        except Exception as e:
            logger.error("ChromaDB get-by-id failed: %s", e)
            return []

        candidates = []
        if results and results.get("ids"):
            for i, doc_id in enumerate(results["ids"]):
                meta = results["metadatas"][i] if results.get("metadatas") else {}
                doc = results["documents"][i] if results.get("documents") else ""
                cid = meta.get("community_id") if meta else None
                if cid is None:
                    try:
                        cid = int(doc_id.split("_")[-1])
                    except (ValueError, IndexError):
                        raise ValueError(f"Invalid community_id format: {doc_id}")
                candidates.append({
                    "community_id": cid,
                    "summary_text": doc or "",
                })
        logger.info(
            "Retrieved %d/%d communities by ID for deterministic scoring",
            len(candidates), len(community_ids),
        )
        return candidates

    def _score_community(self, input_text: str, candidate: dict, mode: str = "pr") -> MapResult:
        """Call LLM to score one community's relevance (thread-safe)."""
        community_id = candidate["community_id"]
        summary_text = candidate["summary_text"]

        if mode == "question":
            sys_prompt = MAP_SYSTEM_PROMPT_QUESTION
            user_template = MAP_USER_TEMPLATE_QUESTION
        else:
            sys_prompt = MAP_SYSTEM_PROMPT_PR
            user_template = MAP_USER_TEMPLATE_PR

        prompt = user_template.format(
            input_text=input_text[:2000],
            community_id=community_id,
            community_summary=summary_text[:3000],
        )

        score, reason = 0, "Could not parse LLM response"
        raw = ""
        try:
            response = self.llm.chat.completions.create(
                model=settings.llm_model,
                messages=[
                    {"role": "system", "content": sys_prompt},
                    {"role": "user",   "content": prompt},
                ],
                max_completion_tokens=2000,
            )
            msg = response.choices[0].message
            finish = response.choices[0].finish_reason

            # GPT-5 / Azure may return None content with refusal or content_filter
            content = msg.content
            if not content:
                refusal = getattr(msg, "refusal", None)
                logger.warning(
                    "Empty content for community %d — finish_reason=%r refusal=%r",
                    community_id, finish, refusal,
                )
                return MapResult(
                    community_id=community_id,
                    score=0,
                    reason=f"Empty response: finish={finish}",
                    summary_text=summary_text,
                )

            raw = content.strip()
            logger.debug("Map score raw for community %d (finish=%s): %r", community_id, finish, raw[:300])

            # Strip markdown code fences (GPT-5 often wraps JSON in ```json ... ```)
            clean = re.sub(r"^```[a-z]*\n?", "", raw).rstrip("` \n")

            # If still no braces, try extracting the first {...} block from the text
            if "{" not in clean:
                m = re.search(r"\{[^}]+\}", raw)
                clean = m.group(0) if m else clean

            parsed = json.loads(clean)
            score  = max(0, min(100, int(parsed.get("score", 0))))
            reason = str(parsed.get("reason", ""))
        except (json.JSONDecodeError, KeyError, ValueError) as e:
            logger.warning(
                "Failed to parse map score for community %d: %s | raw=%r",
                community_id, e, raw[:200],
            )
            score = 0

        return MapResult(
            community_id=community_id,
            score=score,
            reason=reason,
            summary_text=summary_text,
        )
