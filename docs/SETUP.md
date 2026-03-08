# CodeNexus — Setup Guide

## Prerequisites

| Tool | Version | Purpose |
|---|---|---|
| Python | ≥ 3.11 | Runtime (3.11+ required for `tomllib` stdlib; 3.14 tested) |
| Docker + Docker Compose | Latest | Neo4j, ChromaDB, Redis |
| Git | ≥ 2.30 | Repository mirroring |
| ripgrep (`rg`) | ≥ 14.0 | Symbolic lexical search (optional — falls back to `git grep`) |

> **Windows note:** Python is invoked as `py` (the Python Launcher) rather than `python` on
> most Windows installations. All commands in this guide use `py`. On Linux/macOS use `python3`.

---

## Quick Start

```bash
# 1. Clone the repo
git clone <repo-url> nexus && cd nexus/nexus

# 2. (Windows) Bootstrap pip into the embedded project venv
py -m ensurepip --upgrade
py -m pip install --upgrade pip

# 2. (Linux/macOS) Create and activate a virtual environment
# python3 -m venv .venv && source .venv/bin/activate

# 3. Install dependencies
py -m pip install -r requirements.txt

# 4. Configure environment
copy .env.example .env        # Windows
# cp .env.example .env        # Linux/macOS
# Edit .env — at minimum set LLM_API_KEY

# 5. Start infrastructure
docker compose up -d

# 6. Run ingestion
py main.py ingest

# 7. Query the knowledge base
py main.py query "What calls TokenExchangeGrantHandler?"
py main.py explain validateActorToken
py main.py find IMPERSONATED_SUBJECT
```

---

## Configuration

All settings are in `.env` (loaded by `config/settings.py` via pydantic-settings).
See `.env.example` for the complete reference with defaults.

### Critical Settings

| Variable | Required | Description |
|---|---|---|
| `LLM_API_KEY` | **Yes** | OpenAI API key for community summarisation + Map-Reduce |
| `LLM_FAST_MODEL` | No | Model for bulk ops (default: `gpt-4o-mini`) |
| `LLM_STRONG_MODEL` | No | Model for final answers (default: `gpt-4o`) |
| `LLM_PROVIDER` | No | `openai` (default) or `azure` |
| `NEO4J_PASSWORD` | No | Default: `nexuspassword` (matches docker-compose) |
| `GREP_BACKEND` | No | `ripgrep` (default) / `git_grep` / `python` |
| `BLAST_RADIUS_DEPTH` | No | Max graph-traversal hops (default: 3) |
| `LOG_LEVEL` | No | `DEBUG` / `INFO` (default) / `WARNING` / `ERROR` |

### Two-Tier LLM Model Strategy

CodeNexus uses two model tiers to minimise API cost:

| Tier | Env var | Default | Used for |
|---|---|---|---|
| **Fast** | `LLM_FAST_MODEL` | `gpt-4o-mini` | Community summarisation, map-step scoring, query expansion |
| **Strong** | `LLM_STRONG_MODEL` | `gpt-4o` | Final reduce answer, L3 global rollup only |

```bash
# .env example — override model tiers
LLM_FAST_MODEL=gpt-4o-mini
LLM_STRONG_MODEL=gpt-4o
```

All modules call `settings.make_llm_client(tier="fast")` or `settings.make_llm_client(tier="strong")`
instead of constructing `OpenAI()` directly. Switching to Azure requires only `.env` changes.

### Azure OpenAI

```bash
LLM_PROVIDER=azure
LLM_AZURE_ENDPOINT=https://<name>.openai.azure.com
LLM_AZURE_API_VERSION=2025-04-01-preview
LLM_DEPLOYMENT=<your-deployment-name>
LLM_API_KEY=<azure-api-key>
```

### Scaling Settings (100+ repos)

| Variable | Default | Tuning |
|---|---|---|
| `BATCH_SIZE` | 500 | Increase to 2000 for large graphs |
| `MAX_CONCURRENT_REPOS` | 5 | Increase if you have many small repos |
| `SUMMARIZER_MAX_WORKERS` | 4 | Increase up to your OpenAI rate limit |
| `LEIDEN_GAMMA` | 1.5 | Higher = smaller, more granular communities |
| `MAX_CONTEXT_TOKENS` | 8000 | Hard token ceiling per LLM call |

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
- **GDS**: `gds.leiden.write` for community detection, `gds.shortestPath.dijkstra.stream` for
  EntryPoint→DataSink flow extraction

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
py main.py ingest

# Free-form query (uses three-tier router)
py main.py query "Can I remove IMPERSONATED_SUBJECT?"
py main.py query "How does OAuth token refresh work?"

# Explain a method (source + callers + LLM explanation)
py main.py explain validateActorToken
py main.py explain org.wso2.carbon.identity.oauth2.token.handler.TokenExchangeGrantHandler

# Show full call chain (no LLM — pure graph traversal)
py main.py trace TokenExchangeGrantHandler --depth 3

# List all callers of a method
py main.py callers isImpersonationRequest --depth 2 --code

# Exact text search across mirror (no LLM)
py main.py find "throw new OAuthSystemException"
py main.py find ACTOR_TOKEN_REQUIRED --context 10

# Root-cause analysis from exception
py main.py debug NullPointerException
py main.py debug "$(cat stacktrace.txt)"
```

### Alternative: `nexus` CLI (installed as a package)

```bash
py -m pip install -e .        # installs the nexus CLI entry point

nexus ingest
nexus stats
nexus validate --check dangling-calls
nexus analyze-pr "PR summary text here"
```

---

## Running Tests

```bash
# Unit tests (no Docker needed)
py -m pytest tests/ -v --tb=short -k "not infrastructure and not knowledge_base"

# All tests (requires Docker services running)
py -m pytest tests/ -v --tb=short
```

Test files:

| File | What it tests |
|---|---|
| `test_00_infrastructure.py` | Neo4j, ChromaDB, Redis connectivity |
| `test_01_parser.py` | Java AST extraction: classes, methods, annotations, modifiers |
| `test_02_mirror.py` | Git clone/pull |
| `test_03_linker.py` | Maven resolver, API bridge, JAX-RS path composition |
| `test_04_knowledge_base.py` | Integration: ingest → verify Neo4j + ChromaDB |
| `test_05_map_reduce.py` | Map/Reduce reasoning with sample queries |
| `test_06_incremental.py` | Incremental updater |
| `test_07_community_detection.py` | Leiden + summarisation |
| `test_08_community_prompt.py` | Token budget enforcement (DATA_BUDGET = 6,800 tokens) |

---

## Troubleshooting

### Neo4j connection refused
```
neo4j.exceptions.ServiceUnavailable: Failed to establish connection
```
Wait 30 seconds after `docker compose up` — Neo4j takes time to start.
Verify: `curl http://localhost:7474`.

### ChromaDB collection not found
Run `py main.py ingest` first to create and populate collections.

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
The community summarisation step makes one API call per community (fast model).

### `py` command not found (Windows)
If `py` is not available, use the full path: `C:\Python314\python.exe`.
Or install the Python Launcher from python.org.

### `pip install` Rust/Cargo errors
Some packages (`tokenizers`, used by `sentence-transformers`) require Rust to build from source.
Fix: install `chromadb` first — it ships a pre-built `tokenizers` wheel:
```bash
py -m pip install chromadb
py -m pip install sentence-transformers
py -m pip install -r requirements.txt
```

---

## Dependency Reference

Key packages from `requirements.txt`:

| Package | Version | Purpose |
|---|---|---|
| `pydantic` | ≥2.0,<3.0 | Data models (UIR, settings) |
| `pydantic-settings` | ≥2.0,<3.0 | `.env` loading |
| `tree-sitter` | ≥0.20 | Java AST parsing |
| `tree-sitter-java` | ≥0.20 | Java grammar for tree-sitter |
| `neo4j` | ≥5.0,<6.0 | Neo4j Python driver |
| `chromadb` | ≥0.4.0,<0.6.0 | Vector database client |
| `sentence-transformers` | ≥2.0 | `all-MiniLM-L6-v2` embedding model |
| `redis` | ≥4.0,<6.0 | Pipeline state tracking |
| `openai` | ≥1.0,<2.0 | OpenAI + Azure OpenAI API |
| `lxml` | ≥4.9 | Maven `pom.xml` parsing |
| `tiktoken` | ≥0.5 | Token counting for context budgets |
| `tenacity` | ≥8.0,<10.0 | Retry logic for transient failures |
| `gitpython` | ≥3.1 | Git clone/pull for mirrors |
| `pyyaml` | ≥6.0 | `repos.yaml` parsing |
| `click` | ≥8.0 | CLI framework |
| `pytest` | ≥7.0 | Test runner |
