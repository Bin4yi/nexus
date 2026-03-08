"""
community/global_rollup.py
Microsoft GraphRAG hierarchical rollup — generates Level 2 (Sub-System)
and Level 3 (Global Architecture) summaries from Level 1 community summaries.

Implements the Map-Reduce pyramid described in the Microsoft GraphRAG paper:

    Level 1: Leiden Community Summaries (already exist in ChromaDB)
             └─ individual clusters of tightly-coupled code entities

    Level 2: Sub-System Summaries (generated here)
             └─ Groups of L1 communities by domain (Authentication, Federation, etc.)

    Level 3: Global Architecture Summary (generated here)
             └─ Single master document: the complete architectural narrative

Query routing:
    When a user asks "What is the overarching architecture?", the router
    returns the Level 3 summary directly — no vector search needed.
"""
from __future__ import annotations
import logging
import re
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

import tiktoken

from config.settings import settings
from community.summarizer import COLLECTION_NAME as L1_COLLECTION

logger = logging.getLogger(__name__)

_ENCODER = tiktoken.get_encoding("cl100k_base")

# Collection names for hierarchical summaries
L2_COLLECTION = "l2_subsystem_summaries"
L3_COLLECTION = "l3_global_architecture"

# Token budgets
_SYSTEM_TOKENS = 400
_OUTPUT_RESERVE_L2 = 2000   # Each L2 summary can be richer
_OUTPUT_RESERVE_L3 = 3000   # Global summary is the most detailed
_DATA_BUDGET = settings.max_context_tokens - _SYSTEM_TOKENS

# ── Domain classification heuristics ──────────────────────────────────────────
# Map keywords in L1 summary text to high-level domains.
# This avoids an expensive LLM classification call for domain assignment.
DOMAIN_KEYWORDS = {
    "Authentication": [
        "authenticat", "login", "credential", "password", "basic auth",
        "identity provider", "IdP", "local auth", "federated auth",
        "multi-factor", "MFA", "TOTP", "FIDO", "WebAuthn",
    ],
    "OAuth2 / Token Management": [
        "oauth", "token", "access_token", "refresh_token", "bearer",
        "authorization code", "client_credentials", "grant",
        "token exchange", "JWT", "JWS", "JWE", "opaque token",
        "introspect", "revoke", "consent",
    ],
    "SCIM / User Management": [
        "scim", "user management", "user store", "provisioning",
        "group management", "role", "claim", "user profile",
    ],
    "Federation / SSO": [
        "SAML", "federation", "SSO", "single sign-on",
        "identity federation", "assertion", "WS-Federation",
        "trusted identity", "outbound provisioning",
    ],
    "Session Management": [
        "session", "cookie", "session management", "idle timeout",
        "session termination", "remember me",
    ],
    "Dynamic Client Registration": [
        "DCR", "dynamic client registration", "client registration",
        "service provider", "application registration",
    ],
    "OpenID Connect": [
        "OIDC", "OpenID Connect", "ID token", "UserInfo",
        "discovery", "well-known", "claims",
    ],
    "Configuration & Deployment": [
        "configuration", "deployment.toml", "tenant", "server config",
        "carbon", "registry", "governance",
    ],
    "Data Persistence": [
        "DAO", "database", "JDBC", "persistence", "data access",
        "SQL", "table", "query", "repository pattern",
    ],
    "Event Handling & Notifications": [
        "event", "listener", "handler", "notification", "publish",
        "subscribe", "observer", "callback",
    ],
    "Utilities & Framework": [
        "util", "helper", "common", "base class", "abstract",
        "framework", "carbon kernel", "OSGi", "service component",
    ],
}


@dataclass
class SubSystemSummary:
    """A Level 2 domain-level summary."""
    domain: str
    summary_text: str
    l1_community_ids: list[int]
    community_count: int
    llm_model: str
    generated_at: datetime
    token_count: int


@dataclass
class GlobalArchitectureSummary:
    """The Level 3 master architectural document."""
    summary_text: str
    l2_domains: list[str]
    total_communities: int
    llm_model: str
    generated_at: datetime
    token_count: int


class GlobalRollup:
    """
    Implements the Microsoft GraphRAG hierarchical rollup:
        L1 (Leiden communities) → L2 (Sub-System domains) → L3 (Global Architecture)

    Uses Map-Reduce at each level:
        - Map: score/classify each lower-level summary into a domain
        - Reduce: synthesize all summaries in a domain into a single narrative
    """

    def __init__(self, chroma_client, llm_client=None):
        from openai import OpenAI
        self.chroma = chroma_client
        self.llm = llm_client or settings.make_llm_client()

        # Get or create collections
        self.l1_collection = chroma_client.get_or_create_collection(L1_COLLECTION)
        self.l2_collection = chroma_client.get_or_create_collection(L2_COLLECTION)
        self.l3_collection = chroma_client.get_or_create_collection(L3_COLLECTION)

    def run_full_rollup(self) -> GlobalArchitectureSummary:
        """
        Execute the complete L1 → L2 → L3 rollup pipeline.

        Returns:
            The Level 3 GlobalArchitectureSummary.
        """
        logger.info("Starting Global GraphRAG Rollup (L1 → L2 → L3)")

        # Step 1: Fetch all Level 1 community summaries
        l1_summaries = self._fetch_l1_summaries()
        if not l1_summaries:
            logger.warning("No L1 community summaries found — run Leiden + summarization first")
            return GlobalArchitectureSummary(
                summary_text="No community summaries available.",
                l2_domains=[], total_communities=0,
                llm_model=settings.llm_model,
                generated_at=datetime.now(timezone.utc),
                token_count=0,
            )

        logger.info("Fetched %d Level 1 community summaries", len(l1_summaries))

        # Step 2: Classify L1 summaries into domains
        domain_groups = self._classify_into_domains(l1_summaries)
        logger.info(
            "Classified summaries into %d domains: %s",
            len(domain_groups), list(domain_groups.keys()),
        )

        # Step 3: Generate Level 2 Sub-System summaries (Map-Reduce per domain)
        l2_summaries = self._generate_l2_summaries(domain_groups)
        logger.info("Generated %d Level 2 Sub-System summaries", len(l2_summaries))

        # Step 4: Generate Level 3 Global Architecture summary
        l3_summary = self._generate_l3_summary(l2_summaries, len(l1_summaries))
        logger.info("Generated Level 3 Global Architecture summary (%d tokens)", l3_summary.token_count)

        return l3_summary

    # ── Level 1: Fetch ────────────────────────────────────────────────────────

    def _fetch_l1_summaries(self) -> list[dict]:
        """Fetch all Level 1 community summaries from ChromaDB."""
        try:
            count = self.l1_collection.count()
            if count == 0:
                return []
            # Fetch all documents (ChromaDB get with no filter returns all)
            try:
                result = self.l1_collection.get(
                    limit=count,
                    include=["documents", "metadatas"],
                )
            except Exception as e:
                logger.error("ChromaDB get() failed while fetching L1 summaries: %s", e)
                return []
            summaries = []
            if result["ids"]:
                for i, doc_id in enumerate(result["ids"]):
                    summaries.append({
                        "id": doc_id,
                        "text": result["documents"][i],
                        "metadata": result["metadatas"][i],
                        "community_id": result["metadatas"][i].get("community_id", 0),
                    })
            return summaries
        except Exception as e:
            logger.error("Failed to fetch L1 summaries: %s", e)
            return []

    # ── Level 2: Domain Classification + Sub-System Synthesis ─────────────────

    def _classify_into_domains(self, l1_summaries: list[dict]) -> dict[str, list[dict]]:
        """
        Classify each L1 summary into a high-level domain using keyword matching.
        Summaries that don't match any domain go into 'Other / Uncategorized'.
        """
        domain_groups: dict[str, list[dict]] = defaultdict(list)

        for summary in l1_summaries:
            text = (summary.get("text") or "").lower()
            best_domain = "Other / Uncategorized"
            best_score = 0

            for domain, keywords in DOMAIN_KEYWORDS.items():
                score = sum(1 for kw in keywords if kw.lower() in text)
                if score > best_score:
                    best_score = score
                    best_domain = domain

            domain_groups[best_domain].append(summary)

        return dict(domain_groups)

    def _generate_l2_summaries(
        self, domain_groups: dict[str, list[dict]],
    ) -> list[SubSystemSummary]:
        """
        For each domain, run Map-Reduce over its L1 summaries to produce
        a single Level 2 Sub-System summary.
        """
        l2_summaries: list[SubSystemSummary] = []
        max_workers = min(settings.summarizer_max_workers, len(domain_groups))

        with ThreadPoolExecutor(max_workers=max(1, max_workers)) as executor:
            future_to_domain = {
                executor.submit(self._synthesize_domain, domain, summaries): domain
                for domain, summaries in domain_groups.items()
            }
            for future in as_completed(future_to_domain):
                domain = future_to_domain[future]
                try:
                    l2_summary = future.result()
                    if l2_summary:
                        l2_summaries.append(l2_summary)
                except Exception as e:
                    logger.error("Failed to generate L2 summary for %s: %s", domain, e)

        return l2_summaries

    def _synthesize_domain(
        self, domain: str, l1_summaries: list[dict],
    ) -> Optional[SubSystemSummary]:
        """
        Map-Reduce a set of L1 summaries into a single L2 Sub-System summary.
        """
        # Build prompt from L1 summaries
        prompt = self._build_l2_prompt(domain, l1_summaries)
        token_count = len(_ENCODER.encode(prompt))

        system_prompt = (
            "You are a Principal Software Architect creating a sub-system architecture document. "
            f"You are summarizing the '{domain}' sub-system of the WSO2 Identity Server ecosystem. "
            "You receive multiple community-level summaries that together form this sub-system. "
            "Synthesize them into a coherent architectural overview of this sub-system. "
            "Focus on:\n"
            "1. The overall responsibility and scope of this sub-system\n"
            "2. The key components and how they collaborate\n"
            "3. The interfaces with other sub-systems\n"
            "4. Important design patterns used\n"
            "5. Data flow within the sub-system\n\n"
            "Write 3-5 paragraphs of clear, technical prose."
        )

        response = self.llm.chat.completions.create(
            model=settings.llm_model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": prompt},
            ],
            max_completion_tokens=_OUTPUT_RESERVE_L2,
        )
        summary_text = response.choices[0].message.content.strip()

        community_ids = [
            s.get("community_id", 0) for s in l1_summaries
        ]

        l2_summary = SubSystemSummary(
            domain=domain,
            summary_text=summary_text,
            l1_community_ids=community_ids,
            community_count=len(l1_summaries),
            llm_model=settings.llm_model,
            generated_at=datetime.now(timezone.utc),
            token_count=token_count,
        )

        # Store in ChromaDB
        self._upsert_l2(l2_summary)
        return l2_summary

    def _build_l2_prompt(self, domain: str, l1_summaries: list[dict]) -> str:
        """Build the prompt for a Level 2 domain synthesis."""
        lines = [
            f"## Sub-System: {domain}",
            f"Number of code communities: {len(l1_summaries)}",
            "",
            "The following are individual community summaries for code clusters "
            "belonging to this sub-system:",
            "",
        ]

        budget = _DATA_BUDGET - _OUTPUT_RESERVE_L2
        current_tokens = len(_ENCODER.encode("\n".join(lines)))

        for i, summary in enumerate(l1_summaries):
            entry = f"### Community {summary.get('community_id', i)}\n{summary.get('text', '')}\n"
            entry_tokens = len(_ENCODER.encode(entry))
            if current_tokens + entry_tokens > budget:
                lines.append(f"\n[... {len(l1_summaries) - i} more communities truncated for budget ...]")
                break
            lines.append(entry)
            current_tokens += entry_tokens

        lines.append(
            "\nSynthesize these communities into a single cohesive "
            f"Sub-System Architecture summary for: {domain}"
        )
        return "\n".join(lines)

    def _upsert_l2(self, summary: SubSystemSummary) -> None:
        """Store an L2 summary in ChromaDB."""
        doc_id = f"l2:{summary.domain}"
        self.l2_collection.upsert(
            ids=[doc_id],
            documents=[summary.summary_text],
            metadatas=[{
                "domain": summary.domain,
                "community_count": summary.community_count,
                "l1_community_ids": ",".join(str(c) for c in summary.l1_community_ids),
                "llm_model": summary.llm_model,
                "generated_at": summary.generated_at.isoformat(),
                "token_count": summary.token_count,
            }],
        )

    # ── Level 3: Global Architecture Synthesis ────────────────────────────────

    def _generate_l3_summary(
        self, l2_summaries: list[SubSystemSummary], total_l1_count: int,
    ) -> GlobalArchitectureSummary:
        """
        Roll up all L2 Sub-System summaries into a single Level 3
        Global Architecture document.
        """
        prompt = self._build_l3_prompt(l2_summaries, total_l1_count)
        token_count = len(_ENCODER.encode(prompt))

        system_prompt = (
            "You are a Distinguished Software Architect writing the definitive "
            "architecture document for the WSO2 Identity Server ecosystem. "
            "You receive summaries of all major sub-systems discovered by automated "
            "analysis of 100+ repositories. Your task is to produce the master "
            "Level 3 Global Architecture Document.\n\n"
            "This document should:\n"
            "1. Open with a high-level overview of the entire system's purpose and scope\n"
            "2. Describe each major sub-system and its role in the larger architecture\n"
            "3. Explain how sub-systems interact and depend on each other\n"
            "4. Identify the key architectural patterns (Microservices, OSGi, Event-Driven, etc.)\n"
            "5. Describe the data flow from user request to persistence and back\n"
            "6. Note cross-cutting concerns (security, configuration, session management)\n"
            "7. Conclude with the overall architectural philosophy and evolution direction\n\n"
            "Write a comprehensive, well-structured document of 8-15 paragraphs.\n"
            "Use markdown headings to organize sections."
        )

        response = self.llm.chat.completions.create(
            model=settings.llm_model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": prompt},
            ],
            max_completion_tokens=_OUTPUT_RESERVE_L3,
        )
        summary_text = response.choices[0].message.content.strip()

        l3_summary = GlobalArchitectureSummary(
            summary_text=summary_text,
            l2_domains=[s.domain for s in l2_summaries],
            total_communities=total_l1_count,
            llm_model=settings.llm_model,
            generated_at=datetime.now(timezone.utc),
            token_count=token_count,
        )

        # Store in ChromaDB
        self._upsert_l3(l3_summary)
        return l3_summary

    def _build_l3_prompt(
        self, l2_summaries: list[SubSystemSummary], total_l1_count: int,
    ) -> str:
        """Build the prompt for the Level 3 global synthesis."""
        lines = [
            "# Global Architecture Rollup",
            f"Total code communities analyzed: {total_l1_count}",
            f"Sub-systems identified: {len(l2_summaries)}",
            "",
            "The following are the sub-system architecture summaries, "
            "each synthesized from multiple code community clusters:",
            "",
        ]

        budget = _DATA_BUDGET - _OUTPUT_RESERVE_L3
        current_tokens = len(_ENCODER.encode("\n".join(lines)))

        for summary in sorted(l2_summaries, key=lambda s: s.community_count, reverse=True):
            entry = (
                f"## {summary.domain} ({summary.community_count} communities)\n"
                f"{summary.summary_text}\n"
            )
            entry_tokens = len(_ENCODER.encode(entry))
            if current_tokens + entry_tokens > budget:
                lines.append("[... remaining sub-systems truncated for token budget ...]")
                break
            lines.append(entry)
            current_tokens += entry_tokens

        lines.append(
            "\nProduce the Level 3 Global Architecture Document that synthesizes "
            "all the above sub-systems into a single, coherent architectural narrative "
            "for the entire WSO2 Identity Server ecosystem."
        )
        return "\n".join(lines)

    def _upsert_l3(self, summary: GlobalArchitectureSummary) -> None:
        """Store the L3 global summary in ChromaDB."""
        doc_id = "l3:global_architecture"
        self.l3_collection.upsert(
            ids=[doc_id],
            documents=[summary.summary_text],
            metadatas=[{
                "l2_domains": ",".join(summary.l2_domains),
                "total_communities": summary.total_communities,
                "llm_model": summary.llm_model,
                "generated_at": summary.generated_at.isoformat(),
                "token_count": summary.token_count,
            }],
        )
        logger.info("Stored Level 3 Global Architecture summary in ChromaDB")

    # ── Query-time retrieval ──────────────────────────────────────────────────

    def get_global_summary(self) -> Optional[str]:
        """
        Retrieve the Level 3 Global Architecture summary.
        Returns None if not yet generated.

        Used by QueryRouter to short-circuit global architecture questions.
        """
        try:
            result = self.l3_collection.get(
                ids=["l3:global_architecture"],
                include=["documents"],
            )
            if result["ids"]:
                return result["documents"][0]
        except Exception as e:
            logger.warning("Failed to retrieve L3 summary: %s", e)
        return None

    def get_subsystem_summary(self, domain: str) -> Optional[str]:
        """Retrieve a Level 2 Sub-System summary by domain name."""
        try:
            result = self.l2_collection.get(
                ids=[f"l2:{domain}"],
                include=["documents"],
            )
            if result["ids"]:
                return result["documents"][0]
        except Exception:
            pass
        return None

    def list_subsystem_domains(self) -> list[str]:
        """Return all available Level 2 domain names."""
        try:
            result = self.l2_collection.get(include=["metadatas"])
            if result["metadatas"]:
                return [m.get("domain", "") for m in result["metadatas"] if m.get("domain")]
        except Exception:
            pass
        return []
