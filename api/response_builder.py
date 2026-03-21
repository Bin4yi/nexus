"""
api/response_builder.py
Assembles a rich QueryResponse from a RouterResult + reduce answer.

P1.1 — Converts the structured data that the QueryRouter already carries
(grep_hits, seed_nodes, affected_nodes, community_ids) into a
developer-friendly response with clickable file:line citations and
blast-radius summaries.
"""
from __future__ import annotations
import uuid
import logging
from typing import Any

from reasoning.router import RouterResult
from reasoning.lexical_search import GrepHit
from api.models import QueryResponse, SourceRef, AffectedNode

logger = logging.getLogger(__name__)


def build_query_response(
    router_result: RouterResult,
    answer: str,
    latency_ms: float = 0.0,
) -> QueryResponse:
    """
    Convert a RouterResult + LLM answer into a rich QueryResponse.

    Args:
        router_result: The full retrieval payload from QueryRouter.route()
        answer:        The final answer text from ReduceStep.run()
        latency_ms:    Total wall-clock time for the request

    Returns:
        QueryResponse with populated sources, affected nodes, and communities.
    """
    sources  = _extract_sources(router_result)
    affected = _extract_affected(router_result)

    # Estimate per-route confidence
    confidence = _route_confidence(router_result.route)

    return QueryResponse(
        answer=answer,
        route=router_result.route,
        confidence=confidence,
        sources=sources,
        affected=affected,
        communities_used=router_result.community_ids or [],
        latency_ms=round(latency_ms, 1),
        query_id=str(uuid.uuid4())[:8],
    )


# ── Private helpers ────────────────────────────────────────────────────────────

def _extract_sources(result: RouterResult) -> list[SourceRef]:
    """Collect SourceRef objects from grep hits and seed nodes."""
    sources: list[SourceRef] = []
    seen_fqns: set[str] = set()

    # 1. Grep hits (Route A) — have exact file:line
    for hit in result.grep_hits:
        if isinstance(hit, GrepHit):
            fqn  = getattr(hit, "fqn", "") or hit.rel_path
            line = getattr(hit, "line_number", None)
            text = getattr(hit, "text", "")
        elif isinstance(hit, dict):
            fqn  = hit.get("fqn", "") or hit.get("file_path", "")
            line = hit.get("line_number")
            text = hit.get("text", "")
        else:
            continue

        key = f"{fqn}:{line}"
        if key not in seen_fqns:
            seen_fqns.add(key)
            sources.append(SourceRef(
                fqn=fqn,
                file_path=_normalise_path(getattr(hit, "rel_path", "") if isinstance(hit, GrepHit) else hit.get("file_path", "")),
                line_number=line,
                snippet=str(text)[:120] if text else None,
                source_type="grep",
            ))

    # 2. Seed nodes (Route B — Neo4j direct lookup)
    for node in result.seed_nodes:
        fqn = node.get("fqn", "")
        if not fqn or fqn in seen_fqns:
            continue
        seen_fqns.add(fqn)
        sources.append(SourceRef(
            fqn=fqn,
            file_path=_normalise_path(node.get("file_path", "")),
            line_number=node.get("start_line"),
            snippet=node.get("grep_hit"),    # "path:line" string from bridge
            source_type="graph",
        ))

    return sources[:30]   # cap — avoid enormous responses


def _extract_affected(result: RouterResult) -> list[AffectedNode]:
    """Convert blast-radius affected_nodes into AffectedNode objects."""
    affected: list[AffectedNode] = []
    for node in result.affected_nodes:
        fqn = node.get("fqn", "")
        if not fqn:
            continue
        affected.append(AffectedNode(
            fqn=fqn,
            file_path=_normalise_path(node.get("file_path", "")),
            hop_depth=node.get("hop", 1),
            risk_level=node.get("blast_radius_risk"),
            edge_type=node.get("edge_type"),
        ))
    return affected[:50]


def _normalise_path(raw: str) -> str:
    """Strip absolute prefix up to and including 'mirror/' for readability."""
    if not raw:
        return ""
    idx = raw.find("mirror/")
    if idx != -1:
        return raw[idx + len("mirror/"):]
    idx = raw.find("mirror\\")
    if idx != -1:
        return raw[idx + len("mirror\\"):].replace("\\", "/")
    return raw


def _route_confidence(route: str) -> float:
    return {
        "symbolic":         1.0,
        "exact":            0.95,
        "global":           0.90,
        "global_l2":        0.85,
        "semantic":         0.70,
        "symbolic_fallback": 0.65,
        "symbolic_no_bridge": 0.60,
        "global_fallback":  0.55,
    }.get(route, 0.70)
