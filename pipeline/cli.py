"""
pipeline/cli.py
CodeNexus CLI — Click commands for managing the ingestion pipeline.

Commands:
  nexus ingest      — Run full 4-stage ingestion pipeline
  nexus update      — Incremental re-index for a single repo (post-PR)
  nexus validate    — Check Neo4j↔ChromaDB GEID sync
  nexus stats       — Show graph statistics
  nexus analyze-pr  — Run Map-Reduce blast-radius analysis on a PR
"""
import json
import logging
import sys
from pathlib import Path
from typing import Optional

import click

from config.settings import settings

logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format=settings.log_format,
    datefmt="%H:%M:%S",
)


@click.group()
@click.version_option("0.1.0", prog_name="nexus")
def cli():
    """CodeNexus — Java Knowledge Base Ingestion Engine"""


# ── nexus ingest ──────────────────────────────────────────────────────────────

@cli.command("ingest")
@click.option(
    "--config", "-c",
    type=click.Path(exists=True, path_type=Path),
    default=None,
    help="Path to repos.yaml manifest (default: from .env)",
)
@click.option(
    "--skip-summarization", is_flag=True, default=False,
    help="Skip LLM community summarization (faster, no API key needed)",
)
def ingest(config: Optional[Path], skip_summarization: bool):
    """Run the full 4-stage ingestion pipeline on all configured repositories."""
    from pipeline.orchestrator import IngestionPipeline
    click.echo("🚀 Starting CodeNexus ingestion pipeline...")
    pipeline = IngestionPipeline()
    stats = pipeline.run(config_path=config, skip_summarization=skip_summarization)
    _print_stats(stats)
    click.echo("✅ Ingestion complete.")


# ── nexus update ─────────────────────────────────────────────────────────────

@cli.command("update")
@click.argument("repo_name")
@click.option(
    "--since", "-s",
    default=None,
    help="Git SHA to diff from (omit to re-parse all files in repo)",
)
@click.option(
    "--no-summarize", is_flag=True, default=False,
    help="Skip community re-summarization",
)
def update(repo_name: str, since: Optional[str], no_summarize: bool):
    """Incrementally re-index a single repo after a PR merge."""
    from pipeline.incremental import IncrementalUpdater
    click.echo(f"🔄 Incremental update: {repo_name} (since: {since or 'all'})")
    updater = IncrementalUpdater()
    stats = updater.update_repo(
        repo_name=repo_name,
        since_sha=since,
        re_summarize=not no_summarize,
    )
    _print_stats(stats)
    click.echo("✅ Incremental update complete.")


# ── nexus validate ────────────────────────────────────────────────────────────

@cli.command("validate")
def validate():
    """Validate Neo4j ↔ ChromaDB GEID consistency."""
    from neo4j import GraphDatabase
    import chromadb
    from config.settings import settings

    click.echo("🔍 Validating Neo4j ↔ ChromaDB sync...")

    driver = GraphDatabase.driver(settings.neo4j_uri, auth=settings.neo4j_auth)
    chroma = chromadb.HttpClient(host=settings.chroma_host, port=settings.chroma_port)

    issues = 0
    with driver.session() as session:
        result = session.run(
            "MATCH (n:LogicUnit) RETURN n.geid AS geid, n.fqn AS fqn"
        )
        records = list(result)

    try:
        intent_col = chroma.get_collection("code_intent")
    except Exception:
        click.secho("⚠️  code_intent collection not found in ChromaDB", fg="yellow")
        return

    for rec in records:
        geid = rec["geid"]
        chroma_result = intent_col.get(where={"geid": geid}, limit=1)
        if not chroma_result["ids"]:
            click.secho(f"  ❌ Missing in ChromaDB: {rec['fqn']}", fg="red")
            issues += 1

    driver.close()

    if issues == 0:
        click.secho(f"✅ All {len(records)} LogicUnits have matching ChromaDB vectors.", fg="green")
    else:
        click.secho(f"⚠️  {issues} / {len(records)} nodes missing from ChromaDB.", fg="yellow")
        sys.exit(1)


# ── nexus stats ───────────────────────────────────────────────────────────────

@cli.command("stats")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
def stats(as_json: bool):
    """Show current knowledge base statistics."""
    from neo4j import GraphDatabase
    import chromadb
    from config.settings import settings

    driver = GraphDatabase.driver(settings.neo4j_uri, auth=settings.neo4j_auth)
    chroma = chromadb.HttpClient(host=settings.chroma_host, port=settings.chroma_port)

    with driver.session() as session:
        counts = {}
        for label in ["Project", "Module", "Component", "LogicUnit"]:
            r = session.run(f"MATCH (n:{label}) RETURN count(n) AS cnt")
            counts[label] = r.single()["cnt"]
        edge_r = session.run("MATCH ()-[r:CALLS]->() RETURN count(r) AS cnt")
        counts["CALLS_edges"] = edge_r.single()["cnt"]
        comm_r = session.run(
            "MATCH (n) WHERE n.community_id IS NOT NULL "
            "RETURN count(DISTINCT n.community_id) AS cnt"
        )
        counts["communities"] = comm_r.single()["cnt"]

    try:
        logic_col  = chroma.get_collection("code_logic")
        intent_col = chroma.get_collection("code_intent")
        counts["chroma_code_logic"]  = logic_col.count()
        counts["chroma_code_intent"] = intent_col.count()
    except Exception:
        counts["chroma_code_logic"]  = 0
        counts["chroma_code_intent"] = 0

    driver.close()

    if as_json:
        click.echo(json.dumps(counts, indent=2))
    else:
        click.echo("\n📊 CodeNexus Knowledge Base Stats")
        click.echo("─" * 38)
        for key, val in counts.items():
            click.echo(f"  {key:<25} {val:>8,}")
        click.echo()


# ── nexus analyze-pr ──────────────────────────────────────────────────────────

@cli.command("analyze-pr")
@click.argument("pr_description")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
def analyze_pr(pr_description: str, as_json: bool):
    """Run Map-Reduce blast-radius analysis on a PR description."""
    import chromadb
    from config.settings import settings
    from reasoning.pipeline import MapReducePipeline

    click.echo("🔬 Running Map-Reduce PR blast-radius analysis...")
    chroma = chromadb.HttpClient(host=settings.chroma_host, port=settings.chroma_port)
    pipeline = MapReducePipeline(chroma)
    result = pipeline.analyze_pr(pr_description)

    if as_json:
        click.echo(json.dumps({
            "review": result["review"],
            "communities_scored": result["communities_scored"],
            "communities_used": result["communities_used"],
        }, indent=2))
    else:
        click.echo(f"\n📋 Communities evaluated: {result['communities_scored']}")
        click.echo(f"📋 Communities with impact: {result['communities_used']}")
        click.echo("\n" + "─" * 60)
        click.echo(result["review"])
        click.echo("─" * 60 + "\n")


# ── Helpers ───────────────────────────────────────────────────────────────────

def _print_stats(stats: dict) -> None:
    for key, val in stats.items():
        click.echo(f"  {key}: {val}")


if __name__ == "__main__":
    cli()
