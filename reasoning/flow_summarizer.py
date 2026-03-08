"""
reasoning/flow_summarizer.py
Generates natural-language "End-to-End Architectural Stories" from
the execution paths extracted by the GDS Dijkstra shortest-path algorithm.

Each flow narrative describes:
    - The business process triggered by the API endpoint
    - The data transformations along the path
    - The microservices / modules crossed
    - The database tables written to and configuration keys consulted

Narratives are stored in a dedicated ChromaDB collection ``flow_narratives``
for semantic retrieval during query time.
"""
from __future__ import annotations
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import tiktoken

from config.settings import settings

logger = logging.getLogger(__name__)

FLOW_NARRATIVES_COLLECTION = "flow_narratives"

_ENCODER = tiktoken.get_encoding("cl100k_base")

# Token budgets
_SYSTEM_TOKENS = 300
_OUTPUT_RESERVE = 1500
_DATA_BUDGET = settings.max_context_tokens - _SYSTEM_TOKENS - _OUTPUT_RESERVE

_SYSTEM_PROMPT = """\
You are a Principal Software Architect analyzing an enterprise Java codebase \
(WSO2 Identity Server ecosystem). You receive a mathematically-derived execution \
path from an API endpoint down to a database table, including the configuration \
keys the code reads along the way.

Your task is to write the "End-to-End Architectural Story" of this execution flow. \
Explain:
1. What business process this flow implements (e.g., OAuth2 token exchange, SCIM user provisioning)
2. The data transformations that happen at each stage
3. Which microservices or modules are crossed
4. How the configuration keys affect the flow's behavior
5. What the database tables store and why

Write in clear, technical prose suitable for an architecture document. \
Be specific — reference actual class names and method purposes. \
Output 3-6 paragraphs."""


@dataclass
class FlowNarrative:
    """A generated narrative for one execution flow."""
    flow_id: str                # deterministic ID for this flow
    entry_point_fqn: str
    data_sink_fqn: str
    path_fqns: list[str]
    config_keys: list[str]
    table_names: list[str]
    narrative_text: str
    llm_model: str
    generated_at: datetime
    token_count: int


class FlowNarrativeSummarizer:
    """
    Generates and stores natural-language narratives for execution flows.

    Takes FlowPath objects from FlowExtractor and:
    1. Builds an LLM prompt with the chronological path + config + table data
    2. Calls the LLM to write the architectural story
    3. Upserts the narrative into ChromaDB ``flow_narratives`` collection
    """

    def __init__(self, chroma_client, llm_client=None):
        from openai import OpenAI
        self.chroma = chroma_client
        self.llm = llm_client or settings.make_llm_client()
        self.collection = chroma_client.get_or_create_collection(
            FLOW_NARRATIVES_COLLECTION,
        )

    def summarize_flows(
        self,
        flows: list,  # list[FlowPath] — avoids circular import
        max_workers: int = 4,
    ) -> list[FlowNarrative]:
        """
        Generate narratives for all flows using parallel LLM calls.

        Args:
            flows:       FlowPath objects from FlowExtractor
            max_workers: Concurrent LLM calls

        Returns:
            List of FlowNarrative objects
        """
        if not flows:
            logger.info("No flows to narrate")
            return []

        logger.info(
            "Generating narratives for %d flows (max_workers=%d)",
            len(flows), max_workers,
        )

        narratives: list[FlowNarrative] = []
        failed = 0

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_flow = {
                executor.submit(self.summarize_single_flow, flow): flow
                for flow in flows
            }
            for future in as_completed(future_to_flow):
                flow = future_to_flow[future]
                try:
                    narrative = future.result()
                    if narrative:
                        narratives.append(narrative)
                except Exception as e:
                    failed += 1
                    logger.error(
                        "Failed to narrate flow %s → %s: %s",
                        flow.entry_point_fqn, flow.data_sink_fqn, e,
                    )

        logger.info(
            "Generated %d flow narratives (%d failed)",
            len(narratives), failed,
        )
        return narratives

    def summarize_single_flow(self, flow) -> Optional[FlowNarrative]:
        """
        Generate a narrative for a single execution flow.

        Args:
            flow: A FlowPath object from FlowExtractor

        Returns:
            FlowNarrative or None if prompt is empty
        """
        prompt = self._build_flow_prompt(flow)
        if not prompt:
            return None

        token_count = len(_ENCODER.encode(_SYSTEM_PROMPT + prompt))

        response = self.llm.chat.completions.create(
            model=settings.llm_model,
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            max_completion_tokens=_OUTPUT_RESERVE,
        )
        narrative_text = response.choices[0].message.content.strip()

        # Build deterministic flow ID
        flow_id = f"flow:{flow.entry_point_fqn}→{flow.data_sink_fqn}"

        narrative = FlowNarrative(
            flow_id=flow_id,
            entry_point_fqn=flow.entry_point_fqn,
            data_sink_fqn=flow.data_sink_fqn,
            path_fqns=flow.path_fqns,
            config_keys=flow.config_keys,
            table_names=flow.table_names,
            narrative_text=narrative_text,
            llm_model=settings.llm_model,
            generated_at=datetime.now(timezone.utc),
            token_count=token_count,
        )

        # Store in ChromaDB
        self._upsert_narrative(narrative)
        return narrative

    # ── Private ───────────────────────────────────────────────────────────────

    def _build_flow_prompt(self, flow) -> str:
        """Build the LLM prompt from a FlowPath."""
        lines = []

        lines.append("## Execution Flow: API Endpoint → Database")
        lines.append("")

        # Entry point
        lines.append(f"**Entry Point (API):** `{flow.entry_point_fqn}`")
        lines.append(f"**Data Sink (DB):** `{flow.data_sink_fqn}`")
        lines.append(f"**Path Length:** {flow.path_length} hops")
        lines.append("")

        # Chronological path
        lines.append("### Execution Path (chronological order):")
        for i, fqn in enumerate(flow.path_fqns):
            detail = flow.path_node_details[i] if i < len(flow.path_node_details) else {}
            kind = detail.get("kind", "")
            labels = detail.get("labels", [])
            label_str = ", ".join(labels) if labels else ""
            doc = (detail.get("docstring") or "")[:200]

            step = f"{i+1}. `{fqn}`"
            if kind:
                step += f" [{kind}]"
            if label_str:
                step += f" ({label_str})"
            lines.append(step)
            if doc:
                lines.append(f"   Doc: {doc}")

        # Configuration keys
        if flow.config_keys:
            lines.append("")
            lines.append("### Configuration Keys Read:")
            for key in flow.config_keys:
                lines.append(f"- `{key}`")

        # Database tables
        if flow.table_names:
            lines.append("")
            lines.append("### Database Tables Accessed:")
            for table in flow.table_names:
                lines.append(f"- `{table}`")

        lines.append("")
        lines.append(
            "Write the End-to-End Architectural Story of this flow. "
            "Explain the business process, data transformations, "
            "microservices crossed, and how configuration affects behavior."
        )

        prompt = "\n".join(lines)

        # Truncate if exceeding budget
        tokens = len(_ENCODER.encode(prompt))
        if tokens > _DATA_BUDGET:
            # Truncate the path details to fit
            while tokens > _DATA_BUDGET and len(lines) > 10:
                lines.pop(-3)  # Remove from the middle
                prompt = "\n".join(lines)
                tokens = len(_ENCODER.encode(prompt))

        return prompt

    def _upsert_narrative(self, narrative: FlowNarrative) -> None:
        """Store the narrative in ChromaDB."""
        self.collection.upsert(
            ids=[narrative.flow_id],
            documents=[narrative.narrative_text],
            metadatas=[{
                "entry_point_fqn": narrative.entry_point_fqn,
                "data_sink_fqn": narrative.data_sink_fqn,
                "path_length": narrative.path_length if hasattr(narrative, 'path_length') else len(narrative.path_fqns),
                "config_keys": ",".join(narrative.config_keys),
                "table_names": ",".join(narrative.table_names),
                "llm_model": narrative.llm_model,
                "generated_at": narrative.generated_at.isoformat(),
                "token_count": narrative.token_count,
            }],
        )
        logger.debug(
            "Upserted flow narrative: %s → %s",
            narrative.entry_point_fqn, narrative.data_sink_fqn,
        )
