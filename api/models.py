"""
api/models.py
Pydantic request/response models for the CodeNexus REST API.

P1.1 — Rich query responses: structured citations with file:line, blast radius,
        confidence, and affected nodes so IDE plugins can render clickable references.
"""
from __future__ import annotations
from typing import Any, Optional
from pydantic import BaseModel, Field


# ── Request models ─────────────────────────────────────────────────────────────

class QueryRequest(BaseModel):
    question: str = Field(..., description="Natural-language question about the codebase")
    min_confidence: float = Field(
        default=0.0, ge=0.0, le=1.0,
        description="Minimum edge confidence for blast-radius traversal (0=all edges).",
    )
    stream: bool = Field(default=False, description="Stream the LLM answer token-by-token.")


# ── Source citation — file:line reference ─────────────────────────────────────

class SourceRef(BaseModel):
    """A clickable file:line reference included in the response."""
    fqn: str
    file_path: str
    line_number: Optional[int] = None
    snippet: Optional[str] = None      # short code excerpt (≤120 chars)
    source_type: str = "graph"         # "graph" | "grep" | "semantic"
    edge_type: Optional[str] = None    # CALLS, INJECTS, etc.


# ── Affected node from blast-radius traversal ─────────────────────────────────

class AffectedNode(BaseModel):
    fqn: str
    file_path: Optional[str] = None
    hop_depth: int = 1
    risk_level: Optional[str] = None   # CRITICAL | HIGH | MEDIUM | LOW
    edge_type: Optional[str] = None


# ── Spec compliance citation ───────────────────────────────────────────────────

class SpecCitation(BaseModel):
    spec_id: str          # e.g. "RFC6749-4.1"
    rfc_number: int
    section_title: str
    match_type: str       # "citation" | "llm"
    confidence: float


# ── Query response (P1.1) ─────────────────────────────────────────────────────

class QueryResponse(BaseModel):
    answer: str
    route: str                         # symbolic | exact | semantic | global
    confidence: float = 1.0
    sources: list[SourceRef] = []
    affected: list[AffectedNode] = []
    communities_used: list[int] = []
    latency_ms: float = 0.0
    query_id: str = ""


# ── Explain response (P1.5) ───────────────────────────────────────────────────

class ExplainResponse(BaseModel):
    fqn: str
    explanation: str
    node_type: Optional[str] = None     # Component | LogicUnit
    blast_radius_risk: Optional[str] = None
    entry_point_score: Optional[float] = None
    community_id: Optional[int] = None
    callers: list[SourceRef] = []
    callees: list[SourceRef] = []
    spec_links: list[SpecCitation] = []
    latency_ms: float = 0.0


# ── Stats response ────────────────────────────────────────────────────────────

class GraphStats(BaseModel):
    components: int = 0
    logic_units: int = 0
    calls_edges: int = 0
    annotated_with_edges: int = 0
    implements_spec_edges: int = 0
    spec_sections: int = 0
    entry_points: int = 0
    data_sinks: int = 0
    communities: int = 0
    chroma_code_intent: int = 0


# ── Health response ───────────────────────────────────────────────────────────

class HealthResponse(BaseModel):
    status: str                  # "ok" | "degraded" | "error"
    neo4j: str = "unknown"
    chroma: str = "unknown"
    redis: str = "unknown"
    pipeline_stage: Optional[str] = None
