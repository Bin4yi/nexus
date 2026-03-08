"""
parsers/reflection.py
Self-reflection LLM loop — enriches Javadoc extractions by asking the LLM
to find missed architectural connections. Max 2 iterations.
"""
from __future__ import annotations
import logging
from typing import Protocol

logger = logging.getLogger(__name__)

NO_ADDITIONS = "NO_ADDITIONS"


class LLMClient(Protocol):
    """Protocol for any LLM client used by the reflection loop."""
    def call(self, prompt: str, max_tokens: int = 500) -> str:
        ...


class ReflectionLoop:
    """
    Wraps an LLM extraction call in a self-reflection loop.

    Architecture:
        Iteration 1: LLM enriches the raw docstring
        Iteration 2: LLM reflects on its own output and appends anything missed

    The loop exits early if the LLM responds with exactly "NO_ADDITIONS".
    The loop is skipped entirely if the input docstring is empty (cost saving).

    Args:
        max_iterations: Maximum number of LLM calls (default 2, per spec)
    """

    def __init__(self, max_iterations: int = 2):
        if max_iterations < 1:
            raise ValueError("max_iterations must be at least 1")
        self.max_iterations = max_iterations

    def run(
        self,
        docstring: str,
        llm: LLMClient,
        context: str = "",
    ) -> str:
        """
        Run the self-reflection loop on a docstring.

        Args:
            docstring: Raw Javadoc description text to enrich
            llm: LLM client implementing the LLMClient protocol
            context: Optional surrounding code context (method body, class name, etc.)

        Returns:
            Enriched docstring. If docstring is empty, returns empty string unchanged.
        """
        # Skip entirely if no docstring — avoids wasted LLM API calls
        if not docstring.strip():
            logger.debug("Skipping reflection loop — empty docstring")
            return docstring

        logger.debug("Starting reflection loop (max_iterations=%d)", self.max_iterations)

        # Iteration 1: initial enrichment
        enrich_prompt = self._build_enrich_prompt(docstring, context)
        result = llm.call(enrich_prompt, max_tokens=500)
        logger.debug("Reflection iteration 1 complete")

        if self.max_iterations == 1:
            return result

        # Iteration 2: self-reflection on the first output
        reflect_prompt = self._build_reflect_prompt(result, context)
        reflection = llm.call(reflect_prompt, max_tokens=500).strip()
        logger.debug("Reflection iteration 2 complete — response: %r", reflection[:80])

        if reflection == NO_ADDITIONS:
            logger.debug("Early exit: LLM found no additions")
            return result

        return result + "\n" + reflection

    def _build_enrich_prompt(self, docstring: str, context: str) -> str:
        ctx_section = f"\nCode context:\n{context}\n" if context else ""
        return (
            f"You are a senior Java architect analyzing code documentation.\n"
            f"{ctx_section}\n"
            f"Original Javadoc:\n{docstring}\n\n"
            f"Rewrite this Javadoc with any missing architectural details, "
            f"design patterns, or important behavioral notes added. "
            f"Keep it concise and technical."
        )

    def _build_reflect_prompt(self, previous_result: str, context: str) -> str:
        ctx_section = f"\nCode context:\n{context}\n" if context else ""
        return (
            f"You are reviewing a Javadoc enrichment for completeness.\n"
            f"{ctx_section}\n"
            f"Previous enrichment result:\n{previous_result}\n\n"
            f"Did you miss any critical architectural connections, entities, "
            f"or claims? If yes, extract and append them as additional sentences. "
            f"If nothing is missing, respond with exactly: {NO_ADDITIONS}"
        )
