"""
parsers/rfc_semantic_matcher.py
LLM-verified RFC section → Java code mapping.

Two-stage pipeline:

Stage 1 — Candidate Retrieval (cheap, fast):
    For each RFC section, query ChromaDB ``code_intent`` collection with the
    section text to get the top-N most similar Java components by embedding
    distance.  This narrows ~100,000 classes down to 5 candidates per section.

Stage 2 — LLM Verification (accurate):
    For each RFC section, send ONE GPT-4o-mini call with:
      - The RFC section title + body text (what the spec requires)
      - All N candidate classes (FQN + their Javadoc/intent text)
    The LLM answers: which of these candidates actually implements this spec?

Why LLM beats pure embeddings here:
    Embeddings measure surface-level semantic similarity.  An LLM can reason
    about protocol flow, grant type handling, endpoint semantics, and class
    responsibilities — even when the Javadoc says something generic like
    "Handles the request".  The LLM reads both the spec and the code description
    and makes a judgment call.

Cost:
    ~800 LLM calls for 14 RFCs (one call per RFC section).
    At GPT-4o-mini pricing (~$0.15/1M input tokens), ~800 sections ×
    ~1500 tokens = $0.18 total.  Negligible.
"""
from __future__ import annotations
import json
import logging
from typing import Optional

from config.settings import settings
from parsers.rfc_parser import RFCSection, SpecImplementsEdge

logger = logging.getLogger(__name__)

# Stage 1: retrieve this many candidates per RFC section from ChromaDB
_RETRIEVAL_TOP_K = 10

# Stage 1: two-tier distance thresholds (overridden by settings at runtime)
# Primary   (≤ 0.65): high-confidence — always include
# Secondary (≤ 0.85): borderline — include with flag so LLM can decide
_RETRIEVAL_DISTANCE_PRIMARY   = 0.65
_RETRIEVAL_DISTANCE_SECONDARY = 0.85

# System prompt — sets the LLM role and output format
_SYSTEM_PROMPT = """\
You are a software compliance analyst. You map IETF RFC specification
requirements to Java class implementations in a large enterprise codebase
(WSO2 Identity Server).

For each task you receive:
- An RFC section (section number, title, and requirement text)
- A list of candidate Java classes with their fully-qualified name and
  description (from Javadoc or class-level comments)

Your job: decide which candidates genuinely IMPLEMENT the RFC requirement.
A class "implements" a requirement if its core responsibility directly
fulfils what the RFC section specifies — not just if it's vaguely related.

Respond ONLY with a JSON array. Each element must have:
  "fqn": fully-qualified class name (exactly as given)
  "implements": true or false
  "confidence": 0.0 to 1.0
  "reason": one sentence explaining your decision

Example:
[
  {"fqn": "org.wso2.carbon.identity.oauth2.token.handlers.grant.AuthorizationCodeGrantHandler",
   "implements": true, "confidence": 0.92,
   "reason": "Handles the authorization code grant flow as specified in RFC 6749 §4.1."},
  {"fqn": "org.wso2.carbon.identity.oauth2.util.OAuth2Util",
   "implements": false, "confidence": 0.85,
   "reason": "Utility helper, not a grant handler."}
]
"""


class RFCSemanticMatcher:
    """
    Maps RFC sections to Java components using ChromaDB retrieval + LLM verification.
    """

    def __init__(
        self,
        embedder,                          # ChromaEmbedder instance
        llm_client=None,                   # OpenAI client (defaults to settings)
        retrieval_top_k: int = _RETRIEVAL_TOP_K,
        retrieval_distance_primary: float | None = None,
        retrieval_distance_secondary: float | None = None,
        llm_confidence_threshold: float = 0.70,  # minimum LLM confidence to draw an edge
    ):
        self.embedder = embedder
        self.llm = llm_client or settings.make_llm_client(tier="fast")
        self.model = settings.llm_fast_model
        self.retrieval_top_k = retrieval_top_k
        # Use settings values as defaults (allow runtime override)
        self.retrieval_distance_primary = (
            retrieval_distance_primary
            if retrieval_distance_primary is not None
            else settings.rfc_distance_primary
        )
        self.retrieval_distance_secondary = (
            retrieval_distance_secondary
            if retrieval_distance_secondary is not None
            else settings.rfc_distance_secondary
        )
        self.llm_confidence_threshold = llm_confidence_threshold

    # ── Public API ────────────────────────────────────────────────────────────

    def match(self, sections: list[RFCSection]) -> list[SpecImplementsEdge]:
        """
        For each RFC section:
          1. Retrieve top-N candidate components from ChromaDB
          2. Ask the LLM which candidates implement the requirement
          3. Return SpecImplementsEdge objects for confirmed matches

        De-duplicates: if a component matches multiple sections of the same RFC,
        keeps the highest-confidence edge.
        """
        if not sections:
            return []

        # (geid, section_spec_id) → best edge so far — section-granular de-dup
        best: dict[tuple[str, str], SpecImplementsEdge] = {}
        verified_count = 0
        skipped_no_candidates = 0

        for sec in sections:
            # Stage 1: candidate retrieval
            candidates = self._retrieve_candidates(sec)
            if not candidates:
                skipped_no_candidates += 1
                continue

            # Stage 2: LLM verification
            verified = self._verify_with_llm(sec, candidates)
            for item in verified:
                fqn  = item.get("fqn", "")
                conf = float(item.get("confidence", 0.0))
                if not item.get("implements"):
                    continue
                if conf < self.llm_confidence_threshold:
                    continue

                # Look up the geid for this FQN from our candidate list
                geid = next(
                    (c["geid"] for c in candidates if c["fqn"] == fqn), ""
                )
                if not geid:
                    continue

                reason  = item.get("reason", "")
                context = (
                    f"RFC {sec.rfc_number} §{sec.section_number}: "
                    f"{sec.section_title} — {reason}"
                )
                edge = SpecImplementsEdge(
                    component_geid=geid,
                    component_fqn=fqn,
                    rfc_number=sec.rfc_number,
                    citation_context=context,
                    match_type="llm",
                    similarity_score=round(conf, 4),
                    section_spec_id=sec.spec_id,
                    section_title=sec.section_title,
                )

                # De-dup at section level: one edge per (component, RFC section)
                key = (geid, sec.spec_id or f"{sec.rfc_number}-{sec.section_number}")
                existing = best.get(key)
                if existing is None or conf > existing.similarity_score:
                    best[key] = edge
                    verified_count += 1

        edges = list(best.values())
        logger.info(
            "RFC LLM matching: %d sections, %d skipped (no candidates), "
            "%d IMPLEMENTS_SPEC edges (confidence≥%.2f)",
            len(sections), skipped_no_candidates,
            len(edges), self.llm_confidence_threshold,
        )
        return edges

    # ── Private ───────────────────────────────────────────────────────────────

    def _retrieve_candidates(self, sec: RFCSection) -> list[dict]:
        """Stage 1: query ChromaDB code_intent for top-N candidate components."""
        try:
            results = self.embedder.semantic_search(
                query=sec.body_text,
                collection="code_intent",
                n_results=self.retrieval_top_k,
            )
        except Exception as e:
            logger.debug("ChromaDB retrieval failed for RFC %d §%s: %s",
                         sec.rfc_number, sec.section_number, e)
            return []

        candidates = []
        for hit in results:
            dist = hit.get("distance", 999.0)
            if dist > self.retrieval_distance_secondary:
                continue  # outside even the loose threshold — discard
            geid = hit.get("geid", "")
            fqn  = hit.get("fqn", "")
            text = hit.get("text", "")
            if geid and fqn:
                # Flag borderline candidates so the LLM prompt can signal lower confidence
                is_borderline = dist > self.retrieval_distance_primary
                candidates.append({
                    "geid": geid,
                    "fqn": fqn,
                    "description": text,
                    "distance": round(dist, 4),
                    "borderline": is_borderline,
                })

        return candidates

    def _verify_with_llm(
        self, sec: RFCSection, candidates: list[dict]
    ) -> list[dict]:
        """
        Stage 2: ask GPT-4o-mini which candidates implement this RFC section.

        Sends ONE call per RFC section containing ALL candidates.
        Returns a list of dicts: {fqn, implements, confidence, reason}.
        """
        # Truncate RFC section body to avoid excessive token use
        rfc_body = sec.body_text[:2000]

        candidate_lines = "\n".join(
            f'{i+1}. FQN: {c["fqn"]}'
            + (" [BORDERLINE MATCH — lower confidence]" if c.get("borderline") else "")
            + f'\n   Description: {c["description"][:300]}'
            for i, c in enumerate(candidates)
        )

        user_prompt = (
            f"RFC {sec.rfc_number}, Section {sec.section_number}: "
            f"{sec.section_title}\n\n"
            f"Requirement text:\n{rfc_body}\n\n"
            f"Candidate Java classes:\n{candidate_lines}\n\n"
            f"Which of these candidates implement this RFC requirement? "
            f"Return a JSON array as described."
        )

        try:
            response = self.llm.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user",   "content": user_prompt},
                ],
                temperature=0.0,
                max_tokens=800,
                response_format={"type": "json_object"},
            )
            raw = response.choices[0].message.content or "{}"
            parsed = json.loads(raw)

            # The LLM might return {"results": [...]} or just [...]
            if isinstance(parsed, list):
                return parsed
            for key in ("results", "candidates", "matches", "items"):
                if key in parsed and isinstance(parsed[key], list):
                    return parsed[key]
            # Fallback: return all values that are lists
            for v in parsed.values():
                if isinstance(v, list):
                    return v
            return []

        except Exception as e:
            logger.warning(
                "LLM verification failed for RFC %d §%s: %s",
                sec.rfc_number, sec.section_number, e,
            )
            return []
