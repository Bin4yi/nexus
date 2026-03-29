# CodeNexus v2 — Operations Handbook

> **Audience:** Engineers deploying, operating, and maintaining CodeNexus in production.
> Assumes familiarity with Docker, Python, and basic database concepts.

---

## Table of Contents

1. [System Architecture](#1-system-architecture)
2. [Infrastructure Components](#2-infrastructure-components)
3. [Configuration Reference](#3-configuration-reference)
4. [Deployment](#4-deployment)
5. [Ingestion Pipeline](#5-ingestion-pipeline)
6. [Keeping the Knowledge Base Current](#6-keeping-the-knowledge-base-current)
7. [Query Routing Internals](#7-query-routing-internals)
8. [Memory and Scaling](#8-memory-and-scaling)
9. [Monitoring and Observability](#9-monitoring-and-observability)
10. [Troubleshooting](#10-troubleshooting)
11. [Security](#11-security)
12. [Backup and Recovery](#12-backup-and-recovery)
13. [Component Reference](#13-component-reference)

---

## 1. System Architecture

CodeNexus is a **GraphRAG** (Graph-augmented Retrieval-Augmented Generation) system for Java
monolith analysis. It answers natural-language questions about large Java codebases by combining
graph traversal, semantic vector search, and LLM synthesis.

### Two-Phase Design

```
┌─────────────────────────────────────────────────────────────┐
│                    INGESTION (offline)                       │
│                                                              │
│  Java repos → Tree-sitter parser → Neo4j (build graph)      │
│                              ↘   ChromaDB (embed vectors)    │
│                                → SQLite (export snapshot)    │
└─────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────┐
│                    QUERY (live API)                          │
│                                                              │
│  Question → Router → SQLite + ChromaDB → LLM → Answer       │
│                                                              │
│  Neo4j is NOT used at query time.                           │
└─────────────────────────────────────────────────────────────┘
```

### The "Offline Builder / Live Reader" Split

| Component | Ingestion Role | Live API Role |
|---|---|---|
| **Neo4j** | Builds the graph, runs Leiden, runs Dijkstra | **Not used** |
| **SQLite** (`data/nexus_graph.db`) | Written at end of ingest | **Primary graph store** |
| **ChromaDB** | Written during embed step | **Primary vector store** |
| **Redis** | Pipeline stage tracking | Session state |

This split means Neo4j can be stopped after ingestion without affecting the live API.

### Data Flow

```
repos.yaml
    │ git clone/pull
    ▼
mirror/  (local repo copies)
    │ Tree-sitter (ProcessPoolExecutor, all CPU cores)
    ▼
UIR objects (Python dataclasses)
    │
    ├──► Neo4j  ←── CALLS, IMPLEMENTS, RESOLVES_TO, IMPLEMENTS_SPEC edges
    │                  │
    │              Leiden algorithm → community_id on every node
    │                  │
    │              Dijkstra → FlowPath objects
    │                  │
    │              Global Rollup → L2/L3 summaries
    │                  │
    │              SQLite Export → data/nexus_graph.db  ◄── Live API reads here
    │
    └──► ChromaDB ←── code_logic, code_intent, community_summaries,
                       flow_narratives, l2_subsystem_summaries, l3_global_architecture
```

---

## 2. Infrastructure Components

### Docker Services

| Service | Image | Port | Purpose |
|---|---|---|---|
| `nexus-neo4j` | `neo4j:5-enterprise` | 7474 (HTTP), 7687 (Bolt) | Graph build tool (ingest only) |
| `nexus-chromadb` | `chromadb/chroma` | 8000 | Vector store (ingest + live) |
| `nexus-redis` | `redis:7-alpine` | 6379 | Pipeline state tracking |
| `nexus-api` | Local build | 8080 | FastAPI REST endpoint |

### File Storage

| Path | Contents | Size (10 repos) | Size (100 repos est.) |
|---|---|---|---|
| `mirror/` | Cloned Git repos | varies | varies |
| `data/nexus_graph.db` | SQLite graph snapshot | ~15 MB | ~150 MB |
| ChromaDB volume | Vector embeddings | ~309 MB | ~3.0 GB |
| Neo4j volume | Full graph DB | ~500 MB | ~5 GB |

---

## 3. Configuration Reference

All settings are loaded from `.env` via `config/settings.py` (pydantic-settings).
Copy `.env.example` to `.env` and edit.

### Critical Settings

| Setting | Default | Description |
|---|---|---|
| `LLM_API_KEY` | _(empty)_ | OpenAI API key. Required for ingest and query. |
| `LLM_PROVIDER` | `openai` | `openai` or `azure` |
| `LLM_FAST_MODEL` | `gpt-4o-mini` | Bulk ops: community summarization, RFC verification, flow narratives |
| `LLM_STRONG_MODEL` | `gpt-4o` | Final user-facing answers, L3 global rollup |
| `NEO4J_PASSWORD` | `nexuspassword` | Must match `docker-compose.yml` |
| `SQLITE_DB_PATH` | `./data/nexus_graph.db` | Where the SQLite snapshot is written and read |

### Performance Settings

| Setting | Default | Tune when |
|---|---|---|
| `MAX_CONCURRENT_REPOS` | `5` | Lower if RAM is tight during ingest |
| `PARSER_FILE_BATCH_SIZE` | `200` | Lower if parser workers crash (OOM) |
| `EMBEDDING_BATCH_SIZE` | `100` | Lower if ChromaDB connection drops mid-upsert |
| `SUMMARIZER_MAX_WORKERS` | `4` | Higher = faster community summarization (OpenAI rate limited) |
| `HYBRID_SEARCH_N_RESULTS` | `20` | Higher = more candidates per route (slower, more thorough) |
| `BLAST_RADIUS_DEPTH` | `3` | BFS hops for impact analysis (higher = broader but slower) |
| `CHUNK_SIZE` | `512` | Max tokens per code chunk |
| `CHUNK_OVERLAP` | `128` | Token overlap between consecutive chunks |

### LLM Model Tiers

CodeNexus uses a two-tier model strategy to balance cost and quality:

| Tier | Setting | Used for |
|---|---|---|
| **Fast** | `LLM_FAST_MODEL=gpt-4o-mini` | Community summarization, RFC verification, map scoring, flow narratives |
| **Strong** | `LLM_STRONG_MODEL=gpt-4o` | Final reduce answer (what the user sees), L3 global rollup |

The fast tier costs ~90% less than the strong tier. Only the answer the user reads uses the strong model.

### Azure OpenAI

```env
LLM_PROVIDER=azure
LLM_AZURE_ENDPOINT=https://<name>.openai.azure.com
LLM_AZURE_API_VERSION=2025-04-01-preview
LLM_DEPLOYMENT=gpt-4o-mini          # default deployment
LLM_QUERY_DEPLOYMENT=gpt-4o         # deployment for final answers
LLM_PARSER_DEPLOYMENT=gpt-4o-mini   # deployment for query parsing
LLM_API_KEY=<azure-api-key>
```

---

## 4. Deployment

### Prerequisites

- Docker Desktop (Windows/Mac) or Docker Engine + Compose (Linux)
- Python 3.11+ with the `py` launcher (Windows) or `python3` (Linux/Mac)
- 8 GB RAM minimum for 100 repos (16 GB recommended)
- 20 GB free disk space

### First-Time Setup

```bash
# 1. Clone the project
git clone <repo-url>
cd nexus/nexus

# 2. Start infrastructure
docker-compose up -d

# 3. Wait ~60s for Neo4j to initialise, then verify:
docker-compose ps   # all services should show "running"

# 4. Install Python dependencies
py -m pip install -r requirements.txt   # Windows
# python3 -m pip install -r requirements.txt  # Linux/Mac

# 5. Configure
copy .env.example .env   # Windows
# cp .env.example .env   # Linux/Mac
# Edit .env: set LLM_API_KEY, adjust models if needed

# 6. Add repos to index
# Edit sample_repos/repos.yaml

# 7. Run ingestion
py main.py ingest
```

### Starting and Stopping

```bash
# Start everything (Neo4j + ChromaDB + Redis + API)
docker-compose up -d

# Start only what the live API needs (no Neo4j)
docker-compose up -d nexus-chromadb nexus-redis nexus-api

# Stop Neo4j after ingestion (live API doesn't need it)
docker-compose stop nexus-neo4j

# Stop everything
docker-compose down

# Stop everything and delete all data
docker-compose down -v
```

### Upgrading

```bash
# 1. Pull latest code
git pull

# 2. Rebuild API container
docker-compose build nexus-api

# 3. Restart API
docker-compose up -d nexus-api

# 4. If pipeline code changed, re-run ingestion
py main.py ingest
```

---

## 5. Ingestion Pipeline

### Overview

The ingestion pipeline runs in a single command:

```bash
py main.py ingest
```

It executes 16 stages sequentially:

| Stage | What happens | Time (10 repos) |
|---|---|---|
| 1. Mirror | `git clone` / `git pull` all repos | 1–5 min |
| 2. Parallel Parse | Tree-sitter across all CPU cores | 10–30 min |
| 3. Chunk | AST sliding window, 512-token windows | (part of parse) |
| 4. Config Parse | TOML, XML, YAML, properties files | 1–2 min |
| 5. Neo4j Load | Bulk MERGE all nodes and edges | 10–20 min |
| 6. OSGi Resolution | `@Component`/`@Reference` → `RESOLVES_TO` edges | 2–5 min |
| 7. Embed | HuggingFace → ChromaDB upsert | 20–60 min |
| 8. RFC Grounding | ChromaDB candidates + GPT-4o-mini verification | 5–15 min |
| 9. Ollama Drafts | Local LLM micro-drafts for EntryPoints (optional) | 5–10 min |
| 10. Leiden | Community detection via Neo4j GDS | 2–5 min |
| 11. Summarize | GPT-4o-mini per community → ChromaDB | 10–20 min |
| 12. Tag | `:EntryPoint` and `:DataSink` labelling | 1 min |
| 13. Dijkstra | Shortest paths EntryPoint→DataSink | 2–5 min |
| 14. Flow Narratives | GPT-4o-mini per flow path → ChromaDB | 5–10 min |
| 15. Global Rollup | L2 sub-system + L3 architecture document | 5–10 min |
| 16. SQLite Export | Neo4j → `data/nexus_graph.db` | ~5 seconds |

**Total: approximately 1–4 hours for 10 repos** depending on hardware and API latency.

### Monitoring Progress

Redis tracks the current pipeline stage. Check it during a run:

```bash
docker exec nexus-redis redis-cli get nexus:pipeline:stage
```

Or watch the log output — each stage logs its start with `INFO pipeline.orchestrator`.

### Resuming a Failed Ingest

Neo4j uses `MERGE` (idempotent) and ChromaDB uses `upsert`. Re-running `py main.py ingest`
after a failure will skip already-processed data and fill in the missing parts.
You do not need to wipe the databases first.

### Cost

| Step | Model | Cost (10 repos) | Cost (100 repos) |
|---|---|---|---|
| RFC verification | gpt-4o-mini | ~$0.18 (fixed, 14 RFCs) | ~$0.18 (fixed) |
| Community summarization | gpt-4o-mini | ~$0.10 | ~$0.30 |
| Flow narratives | gpt-4o-mini | ~$0.05 | ~$0.20 |
| L2/L3 rollup | gpt-4o | ~$0.10 | ~$0.20 |
| **Total** | | **~$0.43** | **~$0.88** |

---

## 6. Keeping the Knowledge Base Current

### Scenario 1: New Repo Added

```bash
# 1. Add repo to manifest
# Edit sample_repos/repos.yaml

# 2. Run full ingest (SQLite export runs automatically at end)
py main.py ingest

# 3. Restart API
docker-compose restart nexus-api
```

### Scenario 2: Code Changed in Existing Repo

Same as adding a new repo — re-run full ingest:

```bash
py main.py ingest
docker-compose restart nexus-api
```

The pipeline re-clones/pulls, re-parses changed files, and produces a fresh SQLite snapshot.

### Scenario 3: SQLite Refresh Only

Neo4j already has the correct graph (e.g. after a manual Cypher edit or after re-running
a single pipeline stage manually). Just re-export:

```bash
py -c "from graph.sqlite_exporter import run_export; run_export()"
docker-compose restart nexus-api
```

Completes in ~5 seconds.

### Scenario 4: Re-embed After Model Change

If you change `EMBEDDING_MODEL` or switch to FastEmbed:

```bash
# Delete existing collections
py -c "
import chromadb
from config.settings import settings
c = chromadb.HttpClient(host=settings.chroma_host, port=settings.chroma_port)
c.delete_collection('code_logic')
c.delete_collection('code_intent')
print('deleted')
"

# Re-run ingest (will re-create and re-embed)
py main.py ingest
```

### Update Frequency Recommendation

| Situation | Recommended action |
|---|---|
| Active development (daily PRs) | Re-ingest weekly or use `pipeline/incremental.py` for PR analysis |
| Stable codebase | Re-ingest monthly |
| New repo onboarded | Re-ingest immediately |
| Emergency (wrong answer given) | Run SQLite export + restart API (if graph is correct); otherwise full re-ingest |

---

## 7. Query Routing Internals

Every question goes through the router (`reasoning/router.py`) which classifies it and
picks the retrieval strategy before calling the LLM.

### Routes

| Route | Trigger | Retrieval method | LLM calls |
|---|---|---|---|
| **SYMBOLIC / GREP** | Specific symbol name found (e.g. `IMPERSONATED_SUBJECT`) | ripgrep → SQLite blast radius → ChromaDB | 0 (retrieval) + 1 (reduce) |
| **EXACT** | Exact class/method FQN matched | SQLite node lookup → blast radius | 0 + 1 |
| **SEMANTIC** | Conceptual question, no exact match | ChromaDB similarity search | 0 + 1 |
| **GLOBAL** | "What is the architecture?" / high-level | Returns pre-computed L3 doc directly | 0 |

### Intent Classification

The router's LLM parser classifies each question into one of 6 intents:

| Intent | Example | Effect on reduce step |
|---|---|---|
| `safety` | "Can X be removed?" | Safety verdict template, property cross-check |
| `code` | "Show me how X works" | Code-focused answer, higher token budget |
| `narrative` | "Explain the OAuth flow" | Narrative prose output |
| `impact` | "What breaks if I change X?" | Blast radius focus |
| `capability` | "Does this support X?" | Capability verdict format |
| `general` | Anything else | Standard answer |

### Hybrid Search

For semantic queries, the system uses **Reciprocal Rank Fusion (RRF)** to combine:
- BM25 keyword score from SQLite FTS5 full-text index
- Cosine similarity score from ChromaDB

RRF formula: `score = 1 / (k + rank)` where `k = RRF_K` (default 60).

---

## 8. Memory and Scaling

### RAM Usage (Docker containers)

| Component | 10 repos | 100 repos |
|---|---|---|
| nexus-api (SQLite + Python) | ~560 MB | ~700 MB |
| nexus-chromadb | ~310 MB | ~3.0 GB |
| nexus-redis | ~3 MB | ~10 MB |
| **Total** | **~870 MB** | **~3.7 GB** |

100 repos fits comfortably in 8 GB RAM.

### Why RAM Is Low (SQLite Migration)

Before the SQLite migration, the live API queried Neo4j directly. Neo4j's JVM heap
required 4–8 GB for large graphs. With SQLite:

- Neo4j is stopped after ingest — its RAM is freed
- `SqliteRetriever` loads only CALLS edges into Python dicts (~20 MB for 100 repos)
- All other queries are lightweight SQLite selects

### Scaling Beyond 100 Repos

| Repos | RAM needed | Action required |
|---|---|---|
| Up to 100 | 4–8 GB | Current setup, no changes |
| 100–300 | 8–16 GB | Increase Docker memory limit |
| 300+ | 16–32 GB | Consider ChromaDB int8 quantization (requires ChromaDB ≥ 0.6) |

**int8 quantization** (when available) cuts ChromaDB RAM from ~30 MB/repo to ~7.5 MB/repo
(4× reduction, <2% quality loss). Requires `chromadb>=0.6.0` and a re-embed run.

---

## 9. Monitoring and Observability

### Health Checks

```bash
# API health
curl http://localhost:8080/health

# ChromaDB
curl http://localhost:8000/api/v1/heartbeat

# Redis
docker exec nexus-redis redis-cli ping   # should return PONG

# SQLite (verify data is present)
py -c "
from config.settings import settings
import sqlite3
with sqlite3.connect(str(settings.sqlite_db_path)) as c:
    n = c.execute('SELECT count(*) FROM nodes').fetchone()[0]
    e = c.execute('SELECT count(*) FROM calls_edges').fetchone()[0]
    print(f'nodes={n}  calls_edges={e}')
"
```

### Docker Stats

```bash
docker stats --no-stream
```

Watch for:
- `nexus-api` memory creeping up over days → restart the container
- `nexus-chromadb` memory near limit → reduce `HYBRID_SEARCH_N_RESULTS`

### Pipeline Stage (During Ingest)

```bash
# Poll current stage
docker exec nexus-redis redis-cli get nexus:pipeline:stage

# Watch continuously
watch -n5 "docker exec nexus-redis redis-cli get nexus:pipeline:stage"
```

### Log Levels

Set `LOG_LEVEL=DEBUG` in `.env` for verbose output during troubleshooting.
Default is `INFO`.

```bash
# View API logs
docker-compose logs -f nexus-api

# View last 100 lines
docker-compose logs --tail=100 nexus-api
```

### Key Metrics to Monitor

| Metric | Where | Alert if |
|---|---|---|
| Query latency | API logs `Latency: Xms` | > 60s consistently |
| ChromaDB RAM | `docker stats` | > 80% of container limit |
| SQLite file age | `ls -la data/nexus_graph.db` | Older than last expected ingest |
| ChromaDB collection count | `curl localhost:8000/api/v1/collections` | `code_logic` or `code_intent` missing |

---

## 10. Troubleshooting

### API returns empty answers or "not found"

**Cause:** SQLite file missing or empty.

```bash
# Check file exists and has data
ls -la nexus/data/nexus_graph.db
py -c "
import sqlite3
with sqlite3.connect('data/nexus_graph.db') as c:
    print(c.execute('SELECT count(*) FROM nodes').fetchone())
"
```

**Fix:** Run the SQLite export, then restart the API:
```bash
py -c "from graph.sqlite_exporter import run_export; run_export()"
docker-compose restart nexus-api
```

---

### `chromadb.errors.InvalidArgumentError: unknown field hnsw:quantization_type`

**Cause:** Your ChromaDB version is < 0.6 and does not support int8 quantization metadata.

**Fix:** The `hnsw:quantization_type` key has been removed from the codebase. If you see this
error, ensure you are on the latest version of the code.

---

### `SqliteRetriever.get_community_summaries_by_ids() takes 3 positional arguments but 4 were given`

**Cause:** Signature mismatch between old `GraphRetriever` interface and `SqliteRetriever`.

**Fix:** Ensure `graph/sqlite_retriever.py` `get_community_summaries_by_ids` signature is:
```python
def get_community_summaries_by_ids(self, chroma_client, community_ids, collection_name="community_summaries"):
```

---

### `Route 'grep' failed` in chat output

**Cause:** An exception in the grep/symbolic route fell back to semantic. Check the error message.
Common causes:
- ripgrep not installed (`rg` not in PATH) → set `GREP_BACKEND=python` in `.env`
- SQLite query error in blast radius computation

---

### Neo4j connection refused during ingest

**Cause:** Neo4j container not running or still starting up.

```bash
docker-compose ps nexus-neo4j      # check status
docker-compose logs nexus-neo4j    # check for startup errors
```

Neo4j takes ~60 seconds to fully start. The pipeline will retry with backoff.

---

### ChromaDB upsert fails with `Connection aborted`

**Cause:** Large method bodies pushing HTTP payload over ChromaDB's buffer.

**Fix:** Reduce `EMBEDDING_BATCH_SIZE` in `.env` (try 50, then 25). The embedder already
splits batches in half on connection errors with exponential backoff.

---

### Ingest runs but community summaries are empty

**Cause:** Leiden community detection found no communities (graph too sparse, wrong relationship types).

**Fix:**
1. Check `gds_leiden_relationships` in `.env` — ensure it contains `CALLS`
2. Verify CALLS edges exist: `docker exec nexus-neo4j cypher-shell -u neo4j -p nexuspassword "MATCH ()-[:CALLS]->() RETURN count(*) AS c"`
3. Increase `LEIDEN_GAMMA` to produce smaller, more numerous communities

---

### Memory pressure during ingest

**Fix options** (in order of impact):
1. Reduce `MAX_CONCURRENT_REPOS` to `2` or `3`
2. Reduce `PARSER_FILE_BATCH_SIZE` to `100`
3. Reduce `EMBEDDING_BATCH_SIZE` to `50`
4. Add more RAM to Docker Desktop (Settings → Resources)

---

## 11. Security

### API Authentication

By default, the API runs with no authentication (dev mode). Set `API_KEYS` in `.env`
to enable Bearer token auth:

```env
API_KEYS=key1,key2,key3
```

Clients must then send: `Authorization: Bearer key1`

Empty `API_KEYS` (default) disables authentication entirely — do not expose the API publicly
without setting this.

### Secrets Management

- Never commit `.env` to version control (`.gitignore` excludes it)
- `LLM_API_KEY` is the only secret that leaves the machine (sent to OpenAI/Azure)
- Neo4j password is internal-only (not exposed outside Docker network)
- ChromaDB has no auth by default — bind to `localhost` only in production

### Network Exposure

By default, `docker-compose.yml` binds services to `0.0.0.0`. For production deployments,
restrict to `127.0.0.1`:

```yaml
ports:
  - "127.0.0.1:8080:8080"   # API — internal only
  - "127.0.0.1:8000:8000"   # ChromaDB — internal only
```

---

## 12. Backup and Recovery

### What to Back Up

| Item | Path | Frequency | Priority |
|---|---|---|---|
| SQLite graph snapshot | `data/nexus_graph.db` | After every ingest | **Critical** |
| ChromaDB volume | Docker volume `nexus_chroma_data` | After every ingest | High |
| `.env` | Project root | On change | High |
| `repos.yaml` | `sample_repos/repos.yaml` | On change | Medium |
| Neo4j volume | Docker volume `nexus_neo4j_data` | After every ingest | Low (rebuild from repos) |

### Backup Commands

```bash
# Backup SQLite (fast — just a file copy)
cp data/nexus_graph.db data/nexus_graph.db.bak

# Backup ChromaDB volume
docker run --rm -v nexus_chroma_data:/data -v $(pwd)/backups:/backup \
  alpine tar czf /backup/chroma_backup.tar.gz /data

# Restore ChromaDB volume
docker run --rm -v nexus_chroma_data:/data -v $(pwd)/backups:/backup \
  alpine tar xzf /backup/chroma_backup.tar.gz -C /
```

### Recovery

**Scenario: SQLite file corrupted or deleted**
```bash
# If Neo4j is still running with the graph:
py -c "from graph.sqlite_exporter import run_export; run_export()"
docker-compose restart nexus-api
# Recovery time: ~5 seconds

# If Neo4j data is also gone:
py main.py ingest
# Recovery time: full pipeline (~4h for 10 repos)
```

**Scenario: ChromaDB data lost**
```bash
# Restore from backup, or re-run ingest (re-embed step will repopulate)
py main.py ingest
```

---

## 13. Component Reference

### Key Files

| File | Role |
|---|---|
| `main.py` | CLI entry point (`py main.py ingest`, `py main.py query "..."`) |
| `chat.py` | Interactive REPL for development/testing |
| `config/settings.py` | All configuration (pydantic-settings, reads `.env`) |
| `pipeline/orchestrator.py` | Runs all 16 ingest stages in order |
| `graph/sqlite_exporter.py` | Exports Neo4j → SQLite after ingest |
| `graph/sqlite_retriever.py` | Live API graph queries (SQLite, no Neo4j) |
| `reasoning/router.py` | Classifies questions, routes to retrieval strategy |
| `reasoning/reduce_step.py` | Final LLM call → synthesises the answer |
| `api/app.py` | FastAPI REST server |

### API Endpoints

| Method | Path | Description |
|---|---|---|
| `GET` | `/health` | Liveness check |
| `POST` | `/query` | Ask a question (`{"question": "..."}`) |
| `POST` | `/query/stream` | Streaming query response (SSE) |
| `GET` | `/stats` | Graph and vector store counts |
| `GET` | `/explain/{fqn}` | Explain a specific class or method |

### Chat CLI Commands

```
/explain <fqn>    — detailed breakdown of a class or method
/stats            — show current graph and ChromaDB counts
/help             — show available commands
exit / quit       — exit the REPL
```

### Environment Variables Quick Reference

```env
# Required
LLM_API_KEY=sk-...

# LLM Models
LLM_PROVIDER=openai             # or azure
LLM_FAST_MODEL=gpt-4o-mini
LLM_STRONG_MODEL=gpt-4o

# Storage paths
SQLITE_DB_PATH=./data/nexus_graph.db
REPOS_MIRROR_PATH=./mirror
REPOS_CONFIG_PATH=./sample_repos/repos.yaml

# ChromaDB
CHROMA_HOST=localhost
CHROMA_PORT=8000

# Neo4j (ingest only)
NEO4J_URI=bolt://localhost:7687
NEO4J_USER=neo4j
NEO4J_PASSWORD=nexuspassword

# Performance
MAX_CONCURRENT_REPOS=5
EMBEDDING_BATCH_SIZE=100
SUMMARIZER_MAX_WORKERS=4

# Optional features
USE_FASTEMBED=false
LOCAL_DRAFTING_ENABLED=false
OSGI_ENABLED=true
RFC_PATH=./rfcs

# API auth (empty = no auth)
API_KEYS=
API_PORT=8080
LOG_LEVEL=INFO
```
