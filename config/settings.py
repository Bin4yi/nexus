"""
config/settings.py
Central pydantic-settings configuration for CodeNexus.

Architectural role:
    Every tunable constant in the system is defined here and loaded from
    a ``.env`` file (or environment variables).  No Python module is
    allowed to hard-code values that belong to operational configuration.

Data-flow position:
    Imported by **every** module at startup via ``from config.settings import settings``.
"""
from __future__ import annotations

from enum import Enum
from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class GrepBackend(str, Enum):
    """Supported lexical-search backends."""
    RIPGREP = "ripgrep"
    GIT_GREP = "git_grep"
    PYTHON = "python"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── Neo4j ─────────────────────────────────────────────────────────────────
    neo4j_uri: str = Field(default="bolt://localhost:7687")
    neo4j_user: str = Field(default="neo4j")
    neo4j_password: str = Field(default="nexuspassword")

    # ── ChromaDB ──────────────────────────────────────────────────────────────
    chroma_host: str = Field(default="localhost")
    chroma_port: int = Field(default=8000)

    # ── Redis ─────────────────────────────────────────────────────────────────
    redis_url: str = Field(default="redis://localhost:6379/0")

    # ── Paths ─────────────────────────────────────────────────────────────────
    repos_mirror_path: Path = Field(default=Path("./mirror"))
    repos_config_path: Path = Field(default=Path("./sample_repos/repos.yaml"))

    # ── LLM ───────────────────────────────────────────────────────────────────
    llm_provider: str = Field(default="openai")
    llm_model: str = Field(default="gpt-4o-mini")
    llm_api_key: str = Field(default="")
    # Azure OpenAI settings (used when llm_provider="azure")
    llm_azure_endpoint: str = Field(
        default="",
        description="Azure OpenAI endpoint, e.g. https://<name>.openai.azure.com",
    )
    llm_azure_api_version: str = Field(
        default="2025-04-01-preview",
        description="Azure OpenAI API version.",
    )
    llm_deployment: str = Field(
        default="",
        description="Azure deployment name. Defaults to llm_model when empty.",
    )

    # ── GraphRAG / GDS ────────────────────────────────────────────────────────
    gds_graph_name: str = Field(default="codenexus-graph")
    max_context_tokens: int = Field(
        default=8000,
        description="Hard token ceiling for every LLM call (prompt + output reserve).",
    )
    reflection_max_iterations: int = Field(default=2)
    community_summarization_enabled: bool = Field(default=True)

    # ── Hybrid Router ─────────────────────────────────────────────────────────
    grep_backend: GrepBackend = Field(
        default=GrepBackend.RIPGREP,
        description="Lexical search backend: ripgrep | git_grep | python.",
    )
    grep_max_hits: int = Field(
        default=50,
        description="Maximum grep results per symbol search.",
    )
    blast_radius_depth: int = Field(
        default=3,
        description="Max graph-traversal hops for blast-radius computation.",
    )
    router_confidence_threshold: float = Field(
        default=0.7,
        description="Minimum classifier confidence to accept a symbolic/exact route.",
    )

    # ── Leiden Community Detection ────────────────────────────────────────────
    leiden_max_levels: int = Field(default=10)
    leiden_gamma: float = Field(
        default=1.5,
        description="Leiden resolution parameter — higher = smaller communities.",
    )
    leiden_theta: float = Field(default=0.01)
    leiden_write_property: str = Field(default="community_id")
    gds_leiden_relationships: str = Field(
        default="CALLS,INJECTS,IMPLEMENTS,EXTENDS,DEPENDS_ON,DECLARES,HAS_METHOD,REMOTE_CALLS,OVERRIDES",
        description=(
            "Comma-separated whitelist of relationship types projected into the "
            "GDS in-memory graph for Leiden community detection.  MUST exclude "
            "high-fan-out utility edges (THROWS, RETURNS, RECEIVES, INSTANTIATES, "
            "HANDLES_EVENT, ANNOTATED_WITH) to prevent 'God Node' collapse."
        ),
    )

    # ── Community Summarization ───────────────────────────────────────────────
    summarizer_max_workers: int = Field(
        default=4,
        description="Concurrent LLM calls for community summarization.",
    )
    community_summary_max_tokens: int = Field(
        default=1000,
        description="Max output tokens for each community summary LLM call.",
    )

    # ── Batch Processing (Scale) ──────────────────────────────────────────────
    batch_size: int = Field(
        default=500,
        description="Batch size for Neo4j APOC periodic.iterate and other bulk ops.",
    )
    max_concurrent_repos: int = Field(
        default=5,
        description="Number of repos to ingest in parallel during Stage 2.",
    )
    parser_file_batch_size: int = Field(
        default=200,
        description="Number of Java files parsed before flushing to Neo4j/ChromaDB.",
    )

    # ── Embeddings ────────────────────────────────────────────────────────────
    embedding_model: str = Field(
        default="all-MiniLM-L6-v2",
        description="HuggingFace sentence-transformer model for ChromaDB embeddings.",
    )
    embedding_batch_size: int = Field(
        default=100,
        description="ChromaDB upsert batch size.",
    )

    # ── Resilience ────────────────────────────────────────────────────────────
    retry_max_attempts: int = Field(
        default=3,
        description="Max retry attempts for Neo4j / LLM / ChromaDB transient failures.",
    )
    retry_backoff_seconds: float = Field(
        default=1.0,
        description="Initial backoff between retries (doubles each attempt).",
    )

    # ── Logging ───────────────────────────────────────────────────────────────
    log_level: str = Field(
        default="INFO",
        description="Root logging level: DEBUG | INFO | WARNING | ERROR.",
    )
    log_format: str = Field(
        default="%(asctime)s  %(levelname)-8s  %(name)s — %(message)s",
        description="Python logging format string.",
    )

    # ── Derived properties ────────────────────────────────────────────────────

    @property
    def chroma_url(self) -> str:
        return f"http://{self.chroma_host}:{self.chroma_port}"

    @property
    def neo4j_auth(self) -> tuple[str, str]:
        return (self.neo4j_user, self.neo4j_password)

    def make_llm_client(self):
        """Return an OpenAI or AzureOpenAI client based on llm_provider.

        Use this factory everywhere instead of instantiating OpenAI() directly
        so that switching between standard OpenAI and Azure requires only a
        change to the .env file.
        """
        if self.llm_provider.lower() == "azure":
            if not self.llm_azure_endpoint:
                raise ValueError(
                    "llm_azure_endpoint must be set when llm_provider='azure'. "
                    "Set LLM_AZURE_ENDPOINT in your .env file."
                )
            from openai import AzureOpenAI
            deployment = self.llm_deployment or self.llm_model
            return AzureOpenAI(
                api_key=self.llm_api_key,
                azure_endpoint=self.llm_azure_endpoint,
                api_version=self.llm_azure_api_version,
            )
        from openai import OpenAI
        return OpenAI(api_key=self.llm_api_key)


# Singleton — import this everywhere
settings = Settings()
