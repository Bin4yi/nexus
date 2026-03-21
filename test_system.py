"""
test_system.py
Interactive test harness for the CodeNexus query pipeline.

Tests all four query routes and the explain endpoint against real
questions about the mirrored WSO2 OAuth2 codebase.

Usage:
    # Run all built-in test questions
    py test_system.py

    # Ask a custom question
    py test_system.py --ask "How does token exchange work?"

    # Test a specific FQN
    py test_system.py --explain "org.wso2.carbon.identity.oauth2.token.handlers.grant.AuthorizationCodeGrantHandler"

    # Run a specific test category
    py test_system.py --suite symbolic
    py test_system.py --suite semantic
    py test_system.py --suite explain
"""
from __future__ import annotations
import argparse
import json
import logging
import sys
import os
import time
from dataclasses import dataclass
from typing import Optional

logging.basicConfig(level=logging.WARNING)  # suppress noisy imports
logger = logging.getLogger(__name__)

sys.path.insert(0, os.path.dirname(__file__))


# ── Test questions ─────────────────────────────────────────────────────────────

@dataclass
class TestQuestion:
    question: str
    category: str          # symbolic | exact | semantic | global | explain
    expected_route: str    # expected router decision
    expected_keywords: list[str]  # words that should appear in answer
    fqn_hint: Optional[str] = None  # for explain tests


TEST_SUITE = [
    # ── Exact (CamelCase class names → exact graph lookup) ───────────────────
    TestQuestion(
        question="What does OAuth2ServiceComponent do?",
        category="exact",
        expected_route="exact",
        expected_keywords=["OAuth2", "component", "service"],
        fqn_hint="org.wso2.carbon.identity.oauth2.internal.OAuth2ServiceComponent",
    ),
    TestQuestion(
        question="Explain OAuthServerConfiguration",
        category="exact",
        expected_route="exact",
        expected_keywords=["configuration", "OAuth", "server"],
    ),
    TestQuestion(
        question="What is TokenExchangeGrantHandler?",
        category="exact",
        expected_route="exact",
        expected_keywords=["token", "exchange", "grant"],
    ),
    TestQuestion(
        question="How does OAuthTokenPersistenceFactory work?",
        category="exact",
        expected_route="exact",
        expected_keywords=["token", "persistence", "factory"],
    ),

    # ── Symbolic (UPPER_SNAKE_CASE constants) ────────────────────────────────
    TestQuestion(
        question="Where is GRANT_TYPE_AUTHORIZATION_CODE used?",
        category="symbolic",
        expected_route="symbolic",
        expected_keywords=["GRANT_TYPE", "authorization", "code"],
    ),
    TestQuestion(
        question="Can I safely remove IMPERSONATED_SUBJECT?",
        category="symbolic",
        expected_route="symbolic",
        expected_keywords=["IMPERSONATED", "subject"],
    ),

    # ── Semantic (natural language, no entity name) ──────────────────────────
    TestQuestion(
        question="How does the authorization code grant flow work in this codebase?",
        category="semantic",
        expected_route="semantic",
        expected_keywords=["authorization", "code", "grant"],
    ),
    TestQuestion(
        question="Which classes handle OAuth2 scope validation?",
        category="semantic",
        expected_route="semantic",
        expected_keywords=["scope", "validat"],
    ),
    TestQuestion(
        question="How are refresh tokens generated and persisted?",
        category="semantic",
        expected_route="semantic",
        expected_keywords=["refresh", "token"],
    ),

    # ── Global (architecture-level) ─────────────────────────────────────────
    TestQuestion(
        question="What is the overall architecture of this system?",
        category="global",
        expected_route="global",
        expected_keywords=["OAuth", "authentication", "identity"],
    ),
    TestQuestion(
        question="Give me an overarching architecture overview of this codebase",
        category="global",
        expected_route="global",
        expected_keywords=["token", "OAuth", "identity"],
    ),
]

EXPLAIN_SUITE = [
    "org.wso2.carbon.identity.oauth2.internal.OAuth2ServiceComponent",
    "org.wso2.carbon.identity.oauth2.token.handlers.grant.AuthorizationCodeGrantHandler",
    "org.wso2.carbon.identity.oauth2.validators.scope.ScopeValidator",
]


# ── Grading ───────────────────────────────────────────────────────────────────

def grade_answer(answer: str, expected_keywords: list[str]) -> tuple[int, list[str]]:
    """Returns (score 0-100, missing keywords)."""
    lower = answer.lower()
    found = [kw for kw in expected_keywords if kw.lower() in lower]
    missing = [kw for kw in expected_keywords if kw.lower() not in lower]
    score = int(100 * len(found) / len(expected_keywords)) if expected_keywords else 100
    return score, missing


def print_separator(char="-", width=70):
    print(char * width)


def print_result(q: TestQuestion, result: dict, latency_ms: float):
    route = result.get("route", "unknown")
    answer = result.get("answer", "")
    sources = result.get("sources", [])
    affected = result.get("affected", [])
    score, missing = grade_answer(answer, q.expected_keywords)

    route_match = "[OK]" if route == q.expected_route else f"[MISS expected {q.expected_route}]"

    print(f"\n[{q.category.upper()}] {q.question}")
    print_separator()
    print(f"  Route:    {route} {route_match}")
    print(f"  Latency:  {latency_ms:.0f}ms")
    print(f"  Sources:  {len(sources)}  |  Affected: {len(affected)}")
    print(f"  Score:    {score}/100", end="")
    if missing:
        print(f"  (missing: {', '.join(missing)})", end="")
    print()
    print()

    # Print answer (wrapped)
    lines = answer.strip().split("\n")
    for line in lines[:12]:   # cap at 12 lines
        print(f"  {line[:100]}")
    if len(lines) > 12:
        print(f"  ... ({len(lines) - 12} more lines)")

    # Top 3 sources
    if sources:
        print()
        print("  Top sources:")
        for src in sources[:3]:
            fqn = src.get("fqn", "")
            line = src.get("line_number", "")
            fp = src.get("file_path", "").split("/")[-1] if src.get("file_path") else ""
            print(f"    {fqn.split('.')[-1][:50]}  [{fp}:{line}]")

    return score


def print_explain_result(fqn: str, result: dict, latency_ms: float):
    print(f"\n[EXPLAIN] {fqn.split('.')[-1]}")
    print_separator()
    print(f"  Type:      {result.get('node_type', '?')}")
    print(f"  Risk:      {result.get('blast_radius_risk', '?')}")
    print(f"  Community: {result.get('community_id', '?')}")
    print(f"  Callers:   {len(result.get('callers', []))}  |  Callees: {len(result.get('callees', []))}")
    print(f"  Specs:     {len(result.get('spec_links', []))} RFC links")
    print(f"  Latency:   {latency_ms:.0f}ms")
    print()
    explanation = result.get("explanation", "")
    for line in explanation.strip().split("\n")[:10]:
        print(f"  {line[:100]}")


# ── Runner ────────────────────────────────────────────────────────────────────

def run_query(router, reduce, question: str) -> tuple[dict, float]:
    from api.response_builder import build_query_response
    from reasoning.map_step import MapResult

    start = time.time()
    result = router.route(question)
    map_results = []
    if result.community_summaries:
        try:
            from reasoning.map_step import MapResult
            map_results = [
                MapResult(
                    community_id=s.get("community_id", 0),
                    summary_text=s.get("summary_text", ""),
                    score=100,
                    reason="pre-retrieved",
                )
                for s in result.community_summaries
            ]
        except Exception as exc:
            print(f"  [WARN] MapResult build failed: {exc}")

    primary_targets = []
    for hit in result.grep_hits:
        if hasattr(hit, "rel_path"):
            primary_targets.append({
                "source": "grep", "fqn": hit.rel_path,
                "file_path": hit.rel_path, "line_number": hit.line_number,
                "text": hit.line_text, "edge_type": "",
            })
    for node in result.seed_nodes:
        primary_targets.append({**node, "source": "graph"})
    for node in result.affected_nodes[:10]:
        primary_targets.append({**node, "source": "graph"})

    answer = reduce.run(
        map_results=map_results,
        query=question,
        primary_targets=primary_targets,
        route=result.route,
    )
    latency_ms = (time.time() - start) * 1000
    response = build_query_response(result, answer, latency_ms)
    return response.model_dump(), latency_ms


def run_explain(fqn: str, retriever, reduce, driver) -> tuple[dict, float]:
    from api.models import ExplainResponse, SourceRef, SpecCitation

    start = time.time()
    with driver.session() as session:
        rec = session.run(
            "MATCH (n {fqn: $fqn}) RETURN labels(n)[0] AS node_type, "
            "n.geid AS geid, n.blast_radius_risk AS blast_radius_risk, "
            "n.entry_point_score AS entry_point_score, n.community_id AS community_id, "
            "n.file_path AS file_path, n.start_line AS start_line, n.end_line AS end_line "
            "LIMIT 1",
            fqn=fqn,
        ).single()

    if not rec:
        return {"error": f"Not found: {fqn}"}, 0

    raw_callers = retriever.get_callers(fqn, depth=1)
    raw_callees = retriever.get_callees(fqn, depth=1)

    spec_links = []
    with driver.session() as session:
        rows = list(session.run(
            "MATCH (n {geid: $geid})-[r:IMPLEMENTS_SPEC]->(ss:SpecSection) "
            "RETURN ss.spec_id AS spec_id, ss.rfc_number AS rfc_number, "
            "ss.section_title AS section_title, r.confidence AS confidence "
            "ORDER BY r.confidence DESC LIMIT 5",
            geid=rec["geid"],
        ))
    for row in rows:
        spec_links.append({
            "spec_id": row["spec_id"] or "",
            "rfc_number": row["rfc_number"] or 0,
            "section_title": row["section_title"] or "",
            "confidence": float(row["confidence"] or 1.0),
        })

    try:
        explanation = reduce.explain(
            fqn=fqn,
            code_snippets=[],
            callers=raw_callers[:8],
            callees=raw_callees[:8],
        )
    except Exception as e:
        explanation = f"(LLM unavailable: {e})"

    latency_ms = (time.time() - start) * 1000
    return {
        "fqn": fqn,
        "node_type": rec["node_type"],
        "blast_radius_risk": rec["blast_radius_risk"],
        "entry_point_score": rec["entry_point_score"],
        "community_id": rec["community_id"],
        "callers": raw_callers[:15],
        "callees": raw_callees[:15],
        "spec_links": spec_links,
        "explanation": explanation,
    }, latency_ms


def main():
    parser = argparse.ArgumentParser(description="CodeNexus system test")
    parser.add_argument("--ask", help="Single custom question")
    parser.add_argument("--explain", help="FQN to explain")
    parser.add_argument("--suite", choices=["symbolic", "exact", "semantic", "global", "explain", "all"],
                        default="all", help="Test suite to run")
    parser.add_argument("--no-llm", action="store_true", help="Skip LLM calls (test routing only)")
    args = parser.parse_args()

    # ── Initialise pipeline ──────────────────────────────────────────────────
    print("Initialising CodeNexus pipeline...")
    import chromadb
    from neo4j import GraphDatabase
    from config.settings import settings
    from reasoning.router import QueryRouter
    from reasoning.reduce_step import ReduceStep
    from reasoning.graph_retriever import GraphRetriever

    chroma = chromadb.HttpClient(host=settings.chroma_host, port=settings.chroma_port)
    driver = GraphDatabase.driver(settings.neo4j_uri, auth=settings.neo4j_auth)
    router = QueryRouter(chroma_client=chroma)
    reduce = ReduceStep()
    retriever = GraphRetriever(chroma_client=chroma)
    print("Ready.\n")

    total_score = 0
    total_tests = 0
    query_tests = 0

    # ── Custom single question ───────────────────────────────────────────────
    if args.ask:
        result, latency = run_query(router, reduce, args.ask)
        q = TestQuestion(args.ask, "custom", result.get("route","?"), [])
        print_result(q, result, latency)
        return

    if args.explain:
        result, latency = run_explain(args.explain, retriever, reduce, driver)
        print_explain_result(args.explain, result, latency)
        return

    # ── Test suites ──────────────────────────────────────────────────────────
    print("=" * 70)
    print("  CodeNexus System Test")
    print("=" * 70)

    # Query tests
    suites_to_run = {q.category for q in TEST_SUITE}
    if args.suite != "all":
        suites_to_run = {args.suite}

    for suite_name in ["symbolic", "exact", "semantic", "global"]:
        if suite_name not in suites_to_run:
            continue
        questions = [q for q in TEST_SUITE if q.category == suite_name]
        if not questions:
            continue

        print("\n" + "=" * 70)
        print(f"  Suite: {suite_name.upper()}")
        print("=" * 70)

        for q in questions:
            try:
                result, latency = run_query(router, reduce, q.question)
                score = print_result(q, result, latency)
                total_score += score
                total_tests += 1
                query_tests += 1
            except Exception as e:
                print(f"\n[{q.category.upper()}] {q.question}")
                print(f"  ERROR: {e}")

    # Explain tests
    if args.suite in ("all", "explain"):
        print("\n" + "=" * 70)
        print("  Suite: EXPLAIN")
        print("=" * 70)
        for fqn in EXPLAIN_SUITE:
            try:
                result, latency = run_explain(fqn, retriever, reduce, driver)
                print_explain_result(fqn, result, latency)
                total_tests += 1
            except Exception as e:
                print(f"\n[EXPLAIN] {fqn}")
                print(f"  ERROR: {e}")

    # ── Final grade ──────────────────────────────────────────────────────────
    if total_tests > 0:
        avg = total_score / max(query_tests, 1)
        print("\n" + "=" * 70)
        print(f"  OVERALL SCORE: {avg:.0f}/100  ({query_tests} query tests, {total_tests - query_tests} explain tests)")
        if avg >= 85:
            print("  Grade: A  -- Production ready")
        elif avg >= 70:
            print("  Grade: B  -- Good, needs more repos for full coverage")
        elif avg >= 50:
            print("  Grade: C  -- Functional, limited by sparse graph")
        else:
            print("  Grade: D  -- Pipeline works but data is insufficient")
        print("=" * 70)

    router.close()
    driver.close()


if __name__ == "__main__":
    main()
