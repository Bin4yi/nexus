"""
llm/local_drafting.py
Local Ollama micro-draft generator — Sprint 4.

Connects to a local Ollama instance to generate 3-sentence responsibility
summaries for critical ``[:EntryPoint]`` Java classes — at $0 cloud cost.

Summaries are stored as ``micro_draft`` properties directly on Neo4j
``Component`` nodes, making them immediately queryable via graph traversal.

Architecture:
    - Only critical EntryPoint classes are sent to Ollama (filters by annotation)
    - Summaries are capped at 3 sentences to keep context window usage minimal
    - Uses Ollama's REST API directly (no dependency on ollama-python)
    - Graceful degradation: if Ollama is offline, logs a warning and continues
"""
from __future__ import annotations
import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional

from config.settings import settings

logger = logging.getLogger(__name__)

# Prompt template for EntryPoint micro-drafts
_MICRO_DRAFT_PROMPT = """\
You are a senior Java architect. Describe the following class in EXACTLY 3 sentences.
Sentence 1: What is the class's primary business responsibility?
Sentence 2: What are the key collaborators or dependencies it relies on?
Sentence 3: What architectural pattern does it implement (e.g., REST endpoint, OAuth handler, DAO)?

Class: {fqn}
Annotations: {annotations}
Methods: {methods}
"""

# Maximum methods to include in the prompt (keep context small)
_MAX_METHODS = 10


class LocalDraftingEngine:
    """
    Uses Ollama to generate 3-sentence micro-drafts for EntryPoint classes.
    Stores results as ``micro_draft`` properties on Neo4j Component nodes.
    """

    def __init__(self, neo4j_driver, llm_client=None):
        try:
            import requests
            self._requests = requests
        except ImportError:
            raise ImportError("requests is required for LocalDraftingEngine. pip install requests")
        self.driver = neo4j_driver

    def run(self, max_workers: int = 2) -> int:
        """
        Generate micro-drafts for all EntryPoint Component nodes that
        don't yet have a ``micro_draft`` property.

        Args:
            max_workers: Concurrent Ollama calls (keep low to avoid OOM on GPU)

        Returns:
            Number of micro-drafts successfully generated.
        """
        entry_points = self._fetch_entry_points()
        if not entry_points:
            logger.info("No EntryPoint components found for micro-drafting")
            return 0

        logger.info(
            "Generating micro-drafts for %d EntryPoint classes via Ollama (%s)",
            len(entry_points), settings.ollama_model,
        )

        # Check Ollama availability
        if not self._is_ollama_available():
            logger.warning(
                "Ollama is not reachable at %s — skipping micro-drafts",
                settings.ollama_base_url,
            )
            return 0

        generated = 0
        failed = 0

        with ThreadPoolExecutor(max_workers=max(1, max_workers)) as executor:
            future_to_ep = {
                executor.submit(self._draft_single, ep): ep
                for ep in entry_points
            }
            for future in as_completed(future_to_ep):
                ep = future_to_ep[future]
                try:
                    draft = future.result()
                    if draft:
                        self._store_draft(ep["geid"], draft)
                        generated += 1
                except Exception as e:
                    failed += 1
                    logger.warning(
                        "Micro-draft failed for %s: %s", ep.get("fqn", "?"), e,
                    )

        logger.info(
            "Micro-drafts complete — %d generated, %d failed", generated, failed,
        )
        return generated

    # ── Private ───────────────────────────────────────────────────────────────

    def _fetch_entry_points(self) -> list[dict]:
        """Fetch EntryPoint Component nodes that need micro-drafts."""
        with self.driver.session() as session:
            result = session.run(
                """
                MATCH (c:EntryPoint:Component)
                WHERE c.micro_draft IS NULL
                  AND c.fqn IS NOT NULL
                OPTIONAL MATCH (c)-[:HAS_METHOD]->(m:LogicUnit)
                WITH c, collect(m.name)[..10] AS method_names
                RETURN c.geid AS geid,
                       c.fqn AS fqn,
                       c.annotations AS annotations,
                       method_names
                LIMIT 100
                """
            )
            return [dict(r) for r in result]

    def _draft_single(self, ep: dict) -> Optional[str]:
        """Call Ollama to generate a 3-sentence micro-draft for one component."""
        fqn = ep.get("fqn", "")
        methods = ep.get("method_names", []) or []
        annotations_raw = ep.get("annotations", "[]") or "[]"

        # Parse annotations (stored as JSON string)
        try:
            ann_list = json.loads(annotations_raw) if isinstance(annotations_raw, str) else []
            ann_names = [a.get("name", "") for a in ann_list if isinstance(a, dict)]
        except Exception:
            ann_names = []

        prompt = _MICRO_DRAFT_PROMPT.format(
            fqn=fqn,
            annotations=", ".join(f"@{a}" for a in ann_names if a) or "none",
            methods=", ".join(methods[:_MAX_METHODS]) or "none",
        )

        payload = {
            "model": settings.ollama_model,
            "prompt": prompt,
            "stream": False,
            "options": {
                "num_predict": 200,  # cap output tokens — 3 sentences is ~60-120 words
                "temperature": 0.2,  # low temperature for consistent technical prose
            },
        }

        response = self._requests.post(
            f"{settings.ollama_base_url}/api/generate",
            json=payload,
            timeout=30,
        )
        response.raise_for_status()
        data = response.json()
        text = data.get("response", "").strip()

        # Truncate to at most 3 sentences
        sentences = [s.strip() for s in text.split(".") if s.strip()]
        draft = ". ".join(sentences[:3])
        if draft and not draft.endswith("."):
            draft += "."

        return draft if draft else None

    def _store_draft(self, geid: str, draft: str) -> None:
        """Write the micro_draft property to the Neo4j Component node."""
        with self.driver.session() as session:
            session.run(
                """
                MATCH (c:Component {geid: $geid})
                SET c.micro_draft = $draft
                """,
                geid=geid,
                draft=draft,
            ).consume()

    def _is_ollama_available(self) -> bool:
        """Ping Ollama API to verify it is reachable."""
        try:
            resp = self._requests.get(
                f"{settings.ollama_base_url}/api/tags",
                timeout=5,
            )
            return resp.status_code == 200
        except Exception:
            return False
