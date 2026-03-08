# CodeNexus — Setup Guide

## Prerequisites

| Tool | Version | Purpose |
|---|---|---|
| Python | ≥ 3.11 | Runtime |
| Docker + Docker Compose | Latest | Neo4j, ChromaDB, Redis |
| Git | ≥ 2.30 | Repository mirroring |
| ripgrep (`rg`) | ≥ 14.0 | Symbolic lexical search (optional — falls back to `git grep`) |

---

## Quick Start

```bash
# 1. Clone the repo
git clone <repo-url> nexus && cd nexus

# 2. Create a virtual environment
python -m venv .venv
.venv\Scripts\activate        # Windows
# source .venv/bin/activate   # Linux/macOS

# 3. Install dependencies
pip install -r requirements.txt

# 4. Configure environment
cp .env.example .env
# Edit .env — at minimum set LLM_API_KEY

# 5. Start infrastructure
docker compose up -d

# 6. Run ingestion
python main.py ingest

# 7. Query the knowledge base
python main.py query "What calls TokenExchangeGrantHandler?"
python main.py explain validateActorToken
python main.py find IMPERSONATED_SUBJECT
```

---

## Configuration

All settings are in `.env` (loaded by `config/settings.py` via pydantic-settings).
See `.env.example` for the complete reference with defaults.

### Critical Settings

| Variable | Required | Description |
|---|---|---|
| `LLM_API_KEY` | **Yes** | OpenAI API key for community summarisation + Map-Reduce |
| `NEO4J_PASSWORD` | No | Default: `nexuspassword` (matches docker-compose) |
| `GREP_BACKEND` | No | `ripgrep` (default) / `git_grep` / `python` |
| `BLAST_RADIUS_DEPTH` | No | Max graph-traversal hops (default: 3) |
| `LOG_LEVEL` | No | `DEBUG` / `INFO` (default) / `WARNING` / `ERROR` |

### Scaling Settings (100+ repos)

| Variable | Default | Tuning |
|---|---|---|
| `BATCH_SIZE` | 500 | Increase to 2000 for large graphs |
| `MAX_CONCURRENT_REPOS` | 5 | Increase if you have many small repos |
| `SUMMARIZER_MAX_WORKERS` | 4 | Increase up to your OpenAI rate limit |
| `LEIDEN_GAMMA` | 1.0 | Higher = smaller, more granular communities |

---

## Infrastructure

### Docker Compose Services

```bash
docker compose up -d
```

| Service | Port | Dashboard |
|---|---|---|
| Neo4j | 7474 (HTTP), 7687 (Bolt) | http://localhost:7474 |
| ChromaDB | 8000 | — |
| Redis | 6379 | — |

Neo4j credentials: `neo4j` / `nexuspassword` (configurable in `.env`).

### Neo4j Plugins

The Docker image includes **APOC** and **GDS** plugins (required):
- **APOC**: `apoc.periodic.iterate` for batch MERGE operations
- **GDS**: `gds.leiden.write` for community detection, `gds.shortestPath.dijkstra.stream` for EntryPoint→DataSink flow extraction (Phase 2)

---

## Repository Configuration

Edit `sample_repos/repos.yaml` to add repositories:

```yaml
repos:
  - url: https://github.com/wso2-extensions/identity-inbound-auth-oauth.git
    branch: master
  - url: https://github.com/wso2-extensions/identity-oauth2-grant-token-exchange.git
    branch: main
```

---

## CLI Commands

```bash
# Ingest all repos
python main.py ingest

# Free-form query (uses three-tier router)
python main.py query "Can I remove IMPERSONATED_SUBJECT?"
python main.py query "How does OAuth token refresh work?"

# Explain a method (source + callers + LLM explanation)
python main.py explain validateActorToken
python main.py explain org.wso2.carbon.identity.oauth2.token.handler.TokenExchangeGrantHandler

# Show full call chain (no LLM — pure graph traversal)
python main.py trace TokenExchangeGrantHandler --depth 3

# List all callers of a method
python main.py callers isImpersonationRequest --depth 2 --code

# Exact text search across mirror (no LLM)
python main.py find "throw new OAuthSystemException"
python main.py find ACTOR_TOKEN_REQUIRED --context 10

# Root-cause analysis from exception
python main.py debug NullPointerException
python main.py debug "$(cat stacktrace.txt)"
```

---

## Troubleshooting

### Neo4j connection refused
```
neo4j.exceptions.ServiceUnavailable: Failed to establish connection
```
Wait 30 seconds after `docker compose up` — Neo4j takes time to start.
Verify: `curl http://localhost:7474`.

### ChromaDB collection not found
Run `python main.py ingest` first to create and populate collections.

### ripgrep not found
Install ripgrep or set `GREP_BACKEND=git_grep` in `.env`.
The system falls back automatically: ripgrep → git grep → Python.

### GDS Leiden fails
Ensure the Neo4j Docker image includes the GDS plugin.
Check `docker compose logs neo4j` for plugin load errors.

### GDS Dijkstra / Flow extraction fails
This requires `:EntryPoint` and `:DataSink` labels in Neo4j (created by
`NodeTagger` during the tagging post-processing step). If you see zero flow
paths, verify that the ingested repos contain REST endpoint annotations
(`@RequestMapping`, `@Path`) and DAO/Repository classes.

### Rate limiting on OpenAI
Reduce `SUMMARIZER_MAX_WORKERS` in `.env` (e.g. to 2).
The community summarisation step makes one API call per community.
