"""
community/models.py
Pydantic models for GraphRAG community summarization.

Defines ``CommunitySummary`` — the serialised output of one Leiden community's
LLM summarisation pass.  Stored in ChromaDB ``community_summaries`` collection
and used by the Map-Reduce reasoning pipeline.
"""
from __future__ import annotations
from datetime import datetime
from typing import Optional
from pydantic import BaseModel, Field


class CommunitySummary(BaseModel):
    """
    LLM-generated summary of a Leiden community cluster.
    Stored in ChromaDB 'community_summaries' collection with GEID bridge.
    """
    community_id: int
    summary_text: str                   # LLM output — e.g. "This community handles JWT token validation"
    node_count: int
    fqn_list: list[str] = Field(default_factory=list)   # all FQNs in this community
    top_fqns: list[str] = Field(default_factory=list)   # top 5 by call-degree
    llm_model: str = ""
    generated_at: datetime = Field(default_factory=datetime.utcnow)
    token_count: int = 0                # prompt token count — checked against 8k limit
    prompt_truncated: bool = False      # True if community nodes were truncated to fit budget
    fqn_hash: str = ""                  # SHA-1[:16] of sorted FQN list — used for incremental skip
