# CodeNexus v2 — Operations Handbook

> **Audience:** Engineers deploying, operating, and maintaining CodeNexus in production.
> Assumes familiarity with Docker, Python, and basic database concepts.

---

## Table of Contents

1. [System Architecture](#1-system-architecture)
2. [Infrastructure Components](#2-infrastructure-components)
3. [Configuration Reference](#3-configuration-reference)
4. [Deployment](#4-deployment)
5. [Ingestion Pipeline — Deep Dive](#5-ingestion-pipeline--deep-dive)
6. [SQLite Graph Database — Schema and Internals](#6-sqlite-graph-database--schema-and-internals)
7. [Query Pipeline — How Every Question Is Answered](#7-query-pipeline--how-every-question-is-answered)
8. [LLM Layer — Prompts, Intents, and Answer Templates](#8-llm-layer--prompts-intents-and-answer-templates)
9. [Keeping the Knowledge Base Current](#9-keeping-the-knowledge-base-current)
10. [Memory and Scaling](#10-memory-and-scaling)
11. [Monitoring and Observability](#11-monitoring-and-observability)
12. [Troubleshooting](#12-troubleshooting)
13. [Security](#13-security)
14. [Backup and Recovery](#14-backup-and-recovery)
15. [Component Reference](#15-component-reference)

---

## 1. System Architecture

CodeNexus is a **GraphRAG** (Graph-augmented Retrieval-Augmented Generation) system for Java
monolith analysis. It answers natural-language questions about large Java codebases by combining
graph traversal, semantic vector search, and LLM synthesis.

### The "Offline Builder / Live Reader" Architecture

The single most important architectural decision is the strict separation of build-time and
query-time concerns:

```
┌─────────────────────────────────────────────────────────────────┐
│                     INGESTION  (offline, ~4h)                   │
│                                                                  │
│  Java repos → Tree-sitter → Neo4j (build, Leiden, Dijkstra)     │
│                         ↘    ChromaDB (embed all method text)   │
│                           → SQLite (export flat snapshot)        │
└─────────────────────────────────────────────────────────────────┘
                                ↓  one-time snapshot
┌─────────────────────────────────────────────────────────────────┐
│                     LIVE API  (always-on)                        │
│                                                                  │
│  Question → Router → SQLite + ChromaDB → LLM → Answer           │
│                                                                  │
│  Neo4j is NOT contacted at query time.                          │
└─────────────────────────────────────────────────────────────────┘
```

| Component | Ingestion Role | Live API Role |
|---|---|---|
| **Neo4j** | Builds graph, runs Leiden community detection, runs Dijkstra path-finding | **Not used** — stopped after ingest |
| **SQLite** (`data/nexus_graph.db`) | Written at end of every ingest | **Primary graph store** — all node/edge queries |
| **ChromaDB** | Receives embedded code chunks + summaries | **Vector store** — semantic and hybrid search |
| **Redis** | Tracks pipeline stage during ingest | Session state for streaming queries |

**Why this split matters:** Neo4j's JVM heap requires 4–8 GB for large graphs. With SQLite,
the live API uses ~560 MB for 10 repos and ~700 MB for 100 repos. Neo4j can be stopped
(`docker-compose stop nexus-neo4j`) the moment ingestion finishes.

### Full Data Flow

```
sample_repos/repos.yaml
         │  git clone / pull
         ▼
    mirror/  ← local copies of all repos
         │
         │  ProcessPoolExecutor (all CPU cores in parallel)
         │  Tree-sitter reads every .java file:
         │    - class/method names and signatures
         │    - Javadoc, method bodies, annotations
         │    - modifiers (public/private/static/abstract/final)
         │    - OSGi lifecycle markers (@Activate, @Deactivate)
         │    - JAX-RS paths, @QueryParam, @PathParam
         │    - what each method calls (CALLS edges)
         ▼
    UIR objects  (Python dataclasses: Project, Module, Component, LogicUnit)
         │
         ├──────────────────────────────────────────────────────────┐
         │                                                          │
         ▼                                                          ▼
      Neo4j                                                     ChromaDB
   (graph structure)                                         (semantic meaning)
         │                                                          │
   CALLS edges                                            code_logic collection:
   IMPLEMENTS edges                                         method body text → 384-dim vectors
   EXTENDS edges                                           (AST sliding window, 512-token chunks,
   DEPENDS_ON edges                                         [Package:][Class:] context prefix)
   RESOLVES_TO edges (OSGi)                                         │
   IMPLEMENTS_SPEC edges (RFC)                            code_intent collection:
   QUERIES_TABLE edges (SQL)                                Javadoc description → 384-dim vectors
         │                                                          │
         ▼                                                          ▼
   Leiden algorithm (GDS)                              GPT-4o-mini summaries of each
   → community_id on every node                        Leiden community → community_summaries
         │
         ▼
   GDS Dijkstra (weighted)
   CALLS=1, REMOTE_CALLS=5
   → FlowPath objects (EntryPoint→DataSink)
         │
         ▼
   GPT-4o-mini flow narratives
   → flow_narratives in ChromaDB
         │
         ▼
   Global Rollup
   → L2 sub-system summaries (GPT-4o-mini)
   → L3 global architecture doc (GPT-4o)
   both stored in ChromaDB
         │
         ▼
   SQLite Export
   → data/nexus_graph.db  ◄──── Live API reads here
```

---

## 2. Infrastructure Components

### Docker Services

| Service | Image | Ports | RAM (10 repos) | RAM (100 repos) |
|---|---|---|---|---|
| `nexus-neo4j` | `neo4j:5-enterprise` | 7474 (HTTP), 7687 (Bolt) | ~1–3 GB (ingest only) | ~5–8 GB (ingest only) |
| `nexus-chromadb` | `chromadb/chroma` | 8000 | ~310 MB | ~3.0 GB |
| `nexus-redis` | `redis:7-alpine` | 6379 | ~3 MB | ~10 MB |
| `nexus-api` | Local build | 8080 | ~560 MB | ~700 MB |

> Neo4j RAM is only consumed during `py main.py ingest`. Stop it after ingest to reclaim memory.

### ChromaDB Collections

| Collection | Contents | Who writes | Who reads |
|---|---|---|---|
| `code_logic` | Method body chunks (512-token windows, cosine space) | `vectorstore/embedder.py` | `SqliteRetriever.hybrid_search()` |
| `code_intent` | Javadoc/description chunks | `vectorstore/embedder.py` | Router semantic search |
| `community_summaries` | One GPT-4o-mini summary per Leiden community | `community/summarizer.py` | Reduce step context |
| `flow_narratives` | End-to-end execution path stories | `reasoning/flow_summarizer.py` | Narrative route context |
| `l2_subsystem_summaries` | Domain-level rollup summaries | `community/global_rollup.py` | Global route context |
| `l3_global_architecture` | Single master architecture document | `community/global_rollup.py` | Route D (Global) — returned directly |

### File Storage

| Path | Contents | ~Size (10 repos) | ~Size (100 repos) |
|---|---|---|---|
| `mirror/` | Cloned Git repos (full source) | varies | varies |
| `data/nexus_graph.db` | SQLite graph snapshot | ~15 MB | ~150 MB |
| `rfcs/*.txt` | IETF RFC text files for specification grounding | ~5 MB | ~5 MB (fixed) |
| ChromaDB Docker volume | Vector embeddings + HNSW index | ~310 MB | ~3.0 GB |
| Neo4j Docker volume | Full graph DB (ingest use only) | ~500 MB | ~5 GB |

---

## 3. Configuration Reference

All settings live in `.env`, loaded via `config/settings.py` (pydantic-settings).
Copy `.env.example` to `.env` before first run.

### Required Settings

| Setting | Description |
|---|---|
| `LLM_API_KEY` | OpenAI API key (`sk-...`). Required for ingest summaries and all query answers. |
| `LLM_PROVIDER` | `openai` (default) or `azure` |

### LLM Model Configuration

CodeNexus uses a **two-tier model strategy**. Only the answer the user reads uses the strong model.

| Setting | Default | Used for |
|---|---|---|
| `LLM_FAST_MODEL` | `gpt-4o-mini` | Community summarization, RFC verification, flow narratives, map scoring |
| `LLM_STRONG_MODEL` | `gpt-4o` | Final reduce answer (every query), L3 global rollup |
| `LLM_PARSER_MODEL` | `gpt-4o-mini` | Query intent + symbol extraction (router) |

Cost implication: ~90% of LLM calls use the fast model. One strong-model call per query.

### Azure OpenAI Configuration

```env
LLM_PROVIDER=azure
LLM_AZURE_ENDPOINT=https://<name>.openai.azure.com
LLM_AZURE_API_VERSION=2025-04-01-preview
LLM_DEPLOYMENT=gpt-4o-mini
LLM_QUERY_DEPLOYMENT=gpt-4o
LLM_PARSER_DEPLOYMENT=gpt-4o-mini
LLM_API_KEY=<azure-api-key>
```

### Storage Settings

| Setting | Default | Description |
|---|---|---|
| `SQLITE_DB_PATH` | `./data/nexus_graph.db` | Path to SQLite graph snapshot. Read by live API. |
| `REPOS_MIRROR_PATH` | `./mirror` | Where git repos are cloned |
| `REPOS_CONFIG_PATH` | `./sample_repos/repos.yaml` | Repo manifest |
| `CHROMA_HOST` | `localhost` | ChromaDB host |
| `CHROMA_PORT` | `8000` | ChromaDB port |
| `NEO4J_URI` | `bolt://localhost:7687` | Neo4j bolt address (ingest only) |
| `NEO4J_USER` | `neo4j` | Neo4j username |
| `NEO4J_PASSWORD` | `nexuspassword` | Must match `docker-compose.yml` |

### Performance Tuning Settings

| Setting | Default | Effect |
|---|---|---|
| `MAX_CONCURRENT_REPOS` | `5` | How many repos parsed in parallel. Lower if OOM during ingest. |
| `PARSER_FILE_BATCH_SIZE` | `200` | Files per parse worker batch. Lower if workers crash. |
| `EMBEDDING_BATCH_SIZE` | `100` | Chunks per ChromaDB upsert. Lower if connection drops. |
| `SUMMARIZER_MAX_WORKERS` | `4` | Parallel threads for community summarization (OpenAI rate-limited). |
| `HYBRID_SEARCH_N_RESULTS` | `20` | Candidate count per route. Higher = more thorough, slower. |
| `BLAST_RADIUS_DEPTH` | `3` | BFS hops for impact analysis. Higher = broader, slower. |
| `CHUNK_SIZE` | `512` | Max tokens per code chunk (AST sliding window). |
| `CHUNK_OVERLAP` | `128` | Token overlap between consecutive windows. |
| `LEIDEN_GAMMA` | `1.5` | Leiden resolution. Higher = smaller, more granular communities. |

### Optional Feature Flags

| Setting | Default | When to enable |
|---|---|---|
| `USE_FASTEMBED` | `false` | Enable GPU-accelerated Nomic embed (768-dim). Requires `pip install fastembed-gpu onnxruntime-gpu`. |
| `FASTEMBED_MODEL` | `nomic-ai/nomic-embed-text-v1.5` | Model for FastEmbed. |
| `LOCAL_DRAFTING_ENABLED` | `false` | Ollama micro-drafts for EntryPoints. Requires `ollama run llama3.2`. |
| `OLLAMA_BASE_URL` | `http://localhost:11434` | Ollama server address. |
| `OLLAMA_MODEL` | `llama3.2:3b` | Local LLM for micro-drafts. |
| `OSGI_ENABLED` | `true` | Parse `@Component`/`@Reference` annotations → `RESOLVES_TO` edges. |
| `RFC_PATH` | `./rfcs` | Directory of RFC `.txt` files for specification grounding. |
| `API_KEYS` | _(empty)_ | Comma-separated Bearer tokens for API auth. Empty = no auth. |
| `LOG_LEVEL` | `INFO` | Set to `DEBUG` for verbose pipeline output. |

---

## 4. Deployment

### Prerequisites

- Docker Desktop ≥ 4.x (Windows/Mac) or Docker Engine + Compose v2 (Linux)
- Python 3.11+ — use `py` launcher on Windows, `python3` on Linux/Mac
- 8 GB RAM minimum for 100 repos (16 GB recommended; 4 GB sufficient for ≤20 repos)
- 20 GB free disk space

### First-Time Setup

```bash
# 1. Start infrastructure
cd C:\Users\Binula\Desktop\nexus\nexus
docker-compose up -d

# 2. Verify all containers are healthy (~60s for Neo4j to initialise)
docker-compose ps

# 3. Install Python dependencies
py -m pip install -r requirements.txt      # Windows
# python3 -m pip install -r requirements.txt  # Linux/Mac

# 4. Configure
copy .env.example .env    # Windows
# cp .env.example .env   # Linux/Mac
# Edit .env: set LLM_API_KEY=sk-...

# 5. Add repos to index
# Edit sample_repos/repos.yaml

# 6. Run ingestion (builds graph + generates SQLite snapshot)
py main.py ingest

# 7. Neo4j is no longer needed by the live API — stop it to free RAM
docker-compose stop nexus-neo4j
```

### Starting and Stopping

```bash
# Start everything (required during ingest)
docker-compose up -d

# Start only what the live API needs (no Neo4j)
docker-compose up -d nexus-chromadb nexus-redis nexus-api

# Stop Neo4j after ingestion completes
docker-compose stop nexus-neo4j

# Start Neo4j again for next ingest
docker-compose start nexus-neo4j

# Stop everything (data preserved)
docker-compose down

# Stop everything and delete all data (full reset)
docker-compose down -v
```

### Upgrading

```bash
git pull
docker-compose build nexus-api
docker-compose up -d nexus-api
# If pipeline code changed: py main.py ingest
```

---

## 5. Ingestion Pipeline — Deep Dive

### Running the Pipeline

```bash
py main.py ingest
```

### The 16 Stages

#### Stage 1: Mirror (`pipeline/mirror.py`)
Reads `sample_repos/repos.yaml` and runs `git clone` (first time) or `git pull` (subsequent).
Repos are cloned into `mirror/<repo-name>/`.

#### Stage 2–3: Parallel Parse + Chunk (`pipeline/ingest.py`)
`ProcessPoolExecutor` distributes `.java` files across all CPU cores in batches of
`PARSER_FILE_BATCH_SIZE` (default 200).

Each worker subprocess:
1. Loads Tree-sitter Java grammar
2. Parses each `.java` file into an AST
3. Extracts: class hierarchy, method signatures, Javadoc, method body text, annotations
   (as structured dicts), modifiers, OSGi lifecycle markers, parameter annotations,
   lambda call bodies, generic types
4. Applies AST-aware sliding window chunker:
   - Method bodies ≤ 512 tokens → single chunk
   - Method bodies > 512 tokens → overlapping windows (512-token windows, 128-token overlap)
   - Every chunk prefixed: `[Package: org.wso2.identity] [Class: TokenHandler]`
   - Javadoc is never windowed — always one `code_intent` chunk
5. Returns plain Python dicts (pickle-safe for IPC)

Main process reassembles UIR objects from all worker results.

#### Stage 4: Config Parse (`parsers/config_parser.py`)
Scans each repo for:
- `deployment.toml` — parsed with `tomllib` (Python 3.11+ stdlib), handles nested tables accurately
- `*.xml` Spring/WSO2 config — XML parser
- `application.yml` — YAML parser
- `*.properties` — key=value parser

Creates `Configuration` nodes and `READS_CONFIG` edges in Neo4j.

#### Stage 5: Neo4j Load (`graph/loader.py`)
Bulk-writes all UIR objects using Cypher `MERGE` (idempotent — safe to re-run).
Every node gets:
- `fqn` — fully qualified Java name (e.g. `org.wso2.carbon.identity.oauth2.OAuthService.getToken`)
- `geid` — 16-char global entity ID: `SHA256(repo_name + "::" + fqn)[:16]`
- `visibility` — `"public"`, `"protected"`, `"private"`, `"package"`
- `is_static`, `is_abstract`, `is_final`, `is_synchronized` — boolean modifiers
- `lifecycle_role` — `"activate"`, `"deactivate"`, `"modified"` (OSGi), or `null`
- `annotations` — JSON string: `[{"name": "Value", "args": {"value": "${key}"}}]`
- `community_id` — set later by Leiden

19 edge types: `CALLS`, `IMPLEMENTS`, `EXTENDS`, `DEPENDS_ON`, `INJECTS`,
`ANNOTATED_WITH`, `THROWS`, `OVERRIDES`, `INSTANTIATES`, `RETURNS`, `RECEIVES`,
`HANDLES_EVENT`, `REMOTE_CALLS`, `CONTAINS`, `QUERIES_TABLE`, `READS_CONFIG`,
`RESOLVES_TO`, `IMPLEMENTS_SPEC`, `DECLARES`

#### Stage 6: OSGi Resolution (`parsers/osgi_parser.py`)
Scans all Java files for OSGi annotations:
- `@Component(service={Interface.class})` — marks the class as an OSGi service provider
- `@Reference Interface fieldName` — marks a field as an OSGi injection point

Builds `[:RESOLVES_TO]` edges from each injection point to its implementation.
These edges represent runtime wiring invisible to normal call-graph analysis —
without them the graph would have gaps wherever OSGi DI is used.

#### Stage 7: Embed (`vectorstore/embedder.py` → ChromaDB)
For each UIR object:
- Each `code_logic` chunk → `SentenceTransformer("all-MiniLM-L6-v2")` → 384-dim vector → ChromaDB `code_logic` collection
- Each `code_intent` chunk (Javadoc) → same model → ChromaDB `code_intent` collection

Batched in groups of `EMBEDDING_BATCH_SIZE`. On `ConnectionAbortedError`, batch splits in half
and retries with exponential backoff (1s, 2s, 4s, 8s cap).

If `USE_FASTEMBED=true`: uses `nomic-ai/nomic-embed-text-v1.5` (768-dim) via ONNX Runtime.
Automatically selects `CUDAExecutionProvider` if a CUDA GPU is available.

#### Stage 8: RFC Grounding (`parsers/rfc_semantic_matcher.py`)
Two-stage pipeline per RFC text file in `./rfcs/`:

**Stage 8a — Candidate Retrieval (free):**
- Split RFC into numbered sections (RFC 6749 → ~98 sections)
- For each section, query ChromaDB `code_intent` for top-5 most similar Java classes
- This narrows ~50,000 classes to 5 candidates per section

**Stage 8b — LLM Verification (GPT-4o-mini, one call per section):**
Each call contains: RFC section title + full text + 5 candidate Java class FQNs + Javadocs.
LLM decides which candidates genuinely implement the requirement.
Returns JSON: `{fqn, implements: true/false, confidence: 0–1, reason}`.
Only candidates with `implements=true` AND `confidence ≥ 0.70` become `[:IMPLEMENTS_SPEC]` edges.
The edge stores `match_type="llm"`, `similarity_score`, and `citation_context`.

**Cost:** ~800 LLM calls for 14 RFCs ≈ **$0.18 flat** (does not scale with repo count).

#### Stage 9: Ollama Micro-Drafts (`llm/local_drafting.py`) — optional
If `LOCAL_DRAFTING_ENABLED=true`: for every `:EntryPoint` Component node, sends a prompt to
the local Ollama server (`llama3.2:3b`) asking for a 3-sentence architectural summary.
The result is stored as a `micro_draft` property on the Neo4j Component node.
`ThreadPoolExecutor` parallelises drafts. Silently skipped if Ollama is unreachable.
**Cost: $0.**

#### Stage 10: Leiden Community Detection (`graph/gds_client.py`)
Runs Neo4j Graph Data Science Leiden algorithm on the projected call graph.
- `LEIDEN_GAMMA` (default 1.5) controls granularity — higher = more, smaller communities
- Only projects relationship types that actually exist in the graph (prevents GDS crashes)
- Assigns a `community_id` integer to every node
- Communities group closely related code (e.g. all OAuth token exchange classes → community 47)

#### Stage 11: Community Summarization (`community/summarizer.py`)
For each Leiden community:
1. Fetch all member nodes (FQNs, Javadocs, file paths)
2. Build a prompt with `prompt_builder.py` — enforces **6,800-token data budget**
   (`max_context = 8,000 − 200 system − 1,000 output reserve = 6,800 data tokens`)
3. Call GPT-4o-mini: produce a plain-English summary of what this community does
4. Store result in ChromaDB `community_summaries` collection
   (keyed as `community_<id>` — e.g. `community_47`)

`ThreadPoolExecutor` with `SUMMARIZER_MAX_WORKERS` parallel threads.

#### Stage 12: Tag (`graph/tagger.py`)
Labels nodes based on heuristics:
- `:EntryPoint` — JAX-RS `@Path` annotations, Spring `@RestController`, `@RequestMapping`, servlet classes
- `:DataSink` — classes named `*Repository`, `*DAO`, `*JdbcTemplate`, JDBC usage patterns

Also sets `entry_point_score` and `blast_radius_risk` properties.

#### Stage 13: Dijkstra Flow Extraction (`graph/flow_extractor.py`)
Projects a weighted graph in Neo4j GDS:
- `CALLS` edge weight = 1.0
- `REMOTE_CALLS` edge weight = 5.0 (cross-service penalty)

Runs GDS Dijkstra from each `:EntryPoint` to each `:DataSink`.
Returns `FlowPath` objects with: node sequence, config keys touched, database tables accessed.

#### Stage 14: Flow Narratives (`reasoning/flow_summarizer.py`)
For each `FlowPath`: calls GPT-4o-mini with the path nodes and their Javadocs.
Produces a natural-language "execution story": "When `POST /token-exchange` is called,
the request flows through `TokenExchangeGrantHandler.issue()` which calls..."
Stored in ChromaDB `flow_narratives` collection.

#### Stage 15: Global Rollup (`community/global_rollup.py`)
- **L2:** Group L1 community summaries by domain → GPT-4o-mini produces sub-system summaries
  (e.g. "OAuth Engine", "User Store", "Token Introspection")
- **L3:** Synthesise all L2 summaries → GPT-4o produces one master Global Architecture Document
Both stored in ChromaDB.

#### Stage 16: SQLite Export (`graph/sqlite_exporter.py`)
Reads the complete Neo4j graph and writes `data/nexus_graph.db`:
- Sets `PRAGMA journal_mode=WAL` and `PRAGMA synchronous=NORMAL` for performance
- Completely rebuilds the DB (DELETE + re-INSERT) — idempotent
- Builds the FTS5 full-text index for BM25 search
- Runs in **~5 seconds** regardless of graph size

### Pipeline Stage Timing (10 repos)

| Stage | Time |
|---|---|
| Mirror (git pull) | 1–5 min |
| Parallel Parse | 10–30 min |
| Neo4j Load | 10–20 min |
| Embed (ChromaDB) | 20–60 min ← main bottleneck |
| RFC Grounding | 5–15 min |
| Leiden + Summarize | 10–25 min |
| Dijkstra + Flow Narratives | 7–15 min |
| Global Rollup | 5–10 min |
| SQLite Export | ~5 sec |
| **Total** | **~1–4 hours** |

### Ingest Cost Summary

| Step | Model | 10 repos | 100 repos |
|---|---|---|---|
| RFC verification (14 RFCs) | gpt-4o-mini | $0.18 | $0.18 (fixed) |
| Community summarization | gpt-4o-mini | $0.10 | $0.30 |
| Flow narratives | gpt-4o-mini | $0.05 | $0.20 |
| L2 rollup | gpt-4o-mini | $0.03 | $0.10 |
| L3 global doc | gpt-4o | $0.07 | $0.10 |
| **Total** | | **~$0.43** | **~$0.88** |

Each **query** costs < $0.01 (one GPT-4o reduce call, ~2,000–6,000 tokens).

---

## 6. SQLite Graph Database — Schema and Internals

`data/nexus_graph.db` is the live API's sole graph store. It is a complete snapshot of the
Neo4j graph exported at the end of every ingest run.

### Tables

#### `nodes`
Stores every `LogicUnit` (method) and `Component` (class) from Neo4j.

```sql
CREATE TABLE nodes (
    fqn               TEXT PRIMARY KEY,  -- e.g. org.wso2.carbon.oauth2.TokenHandler.getToken
    geid              TEXT,              -- 16-char SHA256 hash (shared with ChromaDB chunk IDs)
    node_type         TEXT,              -- 'LogicUnit' | 'Component'
    kind              TEXT,              -- 'class' | 'interface' | 'method' | 'enum' | ...
    file_path         TEXT,             -- relative path in mirror/
    start_line        INTEGER,
    end_line          INTEGER,
    community_id      INTEGER,           -- Leiden cluster ID
    visibility        TEXT,              -- 'public' | 'protected' | 'private' | 'package'
    is_abstract       INTEGER DEFAULT 0, -- boolean (0/1)
    is_static         INTEGER DEFAULT 0,
    is_final          INTEGER DEFAULT 0,
    is_entry_point    INTEGER DEFAULT 0, -- set by tagger.py
    is_data_sink      INTEGER DEFAULT 0, -- set by tagger.py
    entry_point_score REAL,
    blast_radius_risk TEXT               -- 'HIGH' | 'MEDIUM' | 'LOW'
);
```

#### `calls_edges`
All `CALLS` relationships. Loaded entirely into Python dicts at API startup for O(1) BFS.

```sql
CREATE TABLE calls_edges (
    caller_fqn TEXT NOT NULL,
    callee_fqn TEXT NOT NULL,
    PRIMARY KEY (caller_fqn, callee_fqn)
);
```

#### `property_edges`
Which methods read or write which property keys (constants like `IMPERSONATED_SUBJECT`).

```sql
CREATE TABLE property_edges (
    lu_fqn    TEXT NOT NULL,                         -- method FQN
    prop_key  TEXT NOT NULL,                         -- constant name e.g. "IMPERSONATED_SUBJECT"
    direction TEXT NOT NULL CHECK(direction IN ('read','write')),
    PRIMARY KEY (lu_fqn, prop_key, direction)
);
```

Used exclusively by the safety verdict template to answer "is this constant read anywhere?".

#### `datasink_tables`
Which Component classes query which database tables.

```sql
CREATE TABLE datasink_tables (
    component_fqn TEXT NOT NULL,
    table_name    TEXT NOT NULL,
    source_file   TEXT,
    repo_name     TEXT,
    PRIMARY KEY (component_fqn, table_name)
);
```

#### `nodes_fts` (FTS5 virtual table)
Full-text BM25 index for keyword search. Used by `hybrid_search()`.

```sql
CREATE VIRTUAL TABLE nodes_fts USING fts5(
    fqn       UNINDEXED,  -- not indexed, just carried along in results
    searchtext            -- simple class/method name + kind — what FTS5 indexes
);
```

BM25 scores from this table are fused with ChromaDB cosine scores via
Reciprocal Rank Fusion (RRF): `score = 1 / (k + rank)` where `k = 60`.

### Indexes

```sql
CREATE INDEX idx_nodes_file   ON nodes(file_path);
CREATE INDEX idx_nodes_comm   ON nodes(community_id);
CREATE INDEX idx_nodes_type   ON nodes(node_type, kind);
CREATE INDEX idx_calls_callee ON calls_edges(callee_fqn);
CREATE INDEX idx_calls_caller ON calls_edges(caller_fqn);
CREATE INDEX idx_prop_key     ON property_edges(prop_key, direction);
CREATE INDEX idx_prop_lu      ON property_edges(lu_fqn);
```

### In-Memory Call Graph (`SqliteRetriever`)

At API startup, `SqliteRetriever.__init__()` loads **all** CALLS edges into two Python dicts:

```python
_callers: dict[str, list[str]]   # callee_fqn → [list of caller FQNs]
_callees: dict[str, list[str]]   # caller_fqn → [list of callee FQNs]
```

RAM usage: ~50–80 MB for 100 repos (vs. 7–8 GB for Neo4j).
BFS blast radius is then pure Python — no database round-trips per hop.

### `find_node_by_location` — Three-Strategy Resolution

When a grep hit (`file.java:142`) needs to be mapped to a graph node, the retriever
tries three strategies in order:

1. **Exact line-range match:** Find the `LogicUnit` whose `start_line ≤ 142 ≤ end_line` in that file.
   Selects the narrowest enclosing method (`ORDER BY (end_line - start_line)`).
2. **File fallback:** If no line-range match, return any `LogicUnit` in that file.
3. **Component fallback:** If no LogicUnit, return the `Component` (class) for the file.

---

## 7. Query Pipeline — How Every Question Is Answered

Every query flows through: **Classify → Route → Retrieve → Map → Reduce**.

### Step 1: LLM Query Parser (`LLMQueryParser`)

Before any heuristic classification, the router calls GPT-4o-mini (one call, ~200 tokens)
to extract structured intent from the question:

```json
{
  "bucket": "symbolic",
  "confidence": 0.95,
  "symbols": ["IMPERSONATED_SUBJECT"],
  "entity_names": [],
  "clean_query": "can IMPERSONATED_SUBJECT constant be removed",
  "intent": "safety"
}
```

The `intent` field drives which LLM answer template is used in the Reduce step.
Valid intents: `safety`, `code`, `narrative`, `impact`, `capability`, `general`.

Regex fallback (`_SAFETY_RE`, `_CODE_RE`, `_NARRATIVE_RE`, `_CAPABILITY_RE`) is used if
the LLM parser call fails or returns an invalid intent.

### Step 2: QueryClassifier — Four Buckets

| Bucket | Detection heuristic | Example |
|---|---|---|
| `symbolic` | `UPPER_SNAKE_CASE` tokens or quoted string literals | `IMPERSONATED_SUBJECT`, `"token_exchange"` |
| `exact` | `CamelCase` tokens, dotted FQNs, `lowerCamelCase` | `TokenExchangeGrantHandler`, `oauth2.OAuthService` |
| `global` | Keywords: `overarching architecture`, `entire system`, `what is the architecture` | "What is the overarching architecture?" |
| `conceptual` | Everything else | "How does token refresh work?" |

The classifier is **stateless and has no database calls** — safe to unit-test in isolation.

### Step 3: Route Execution

#### Route A — SYMBOLIC / GREP
Triggered by `UPPER_SNAKE_CASE` or quoted literals.

1. `LexicalSearcher` runs ripgrep on `./mirror/` for every symbol
   (`rg --json -n <symbol> mirror/` → JSON lines of file:line matches)
2. For each grep hit: call `find_node_by_location(file, line)` → map to graph node
3. Collect all matched nodes as `seed_nodes`
4. Run `compute_blast_radius(seed_fqns, depth=3)` — BFS over in-memory `_callers` dict
5. Extract `community_ids` from all affected nodes
6. Fetch community summaries from ChromaDB by ID (deterministic, no vector search)
7. Also run `find_property_readers(symbol)` and `find_property_writers(symbol)` — property graph cross-check

**Zero LLM calls in this entire step.** LLM is called only once in Reduce.

#### Route B — EXACT-ENTITY
Triggered by CamelCase entity names.

1. `find_nodes(entity_name)` — SQLite LIKE query on `fqn` column
2. `compute_blast_radius(seed_fqns)` — same BFS
3. `get_community_summaries_by_ids(community_ids)` — ChromaDB fetch by ID

#### Route C — SEMANTIC / CONCEPTUAL
Triggered when no symbols or entities are detected.

1. ChromaDB `code_intent` collection: cosine similarity search for query text → top-20 results
2. SQLite FTS5: BM25 keyword search on `nodes_fts` → top-20 results
3. **Reciprocal Rank Fusion (RRF):** merge both ranked lists:
   `rrf_score = 1/(60 + chroma_rank) + 1/(60 + bm25_rank)`
4. Top merged results become `seed_nodes`
5. Blast radius expansion + community summary fetch (same as Route A/B)

#### Route D — GLOBAL
Triggered by global architecture keywords.

1. Fetch L3 Global Architecture Document directly from ChromaDB `l3_global_architecture`
2. Fetch L2 sub-system summaries from `l2_subsystem_summaries`
3. **Return immediately** — no grep, no graph traversal, no vector search.

Response time for Route D: ~200ms (pure ChromaDB retrieval, no graph work).

### Step 4: Map Step (`reasoning/map_step.py`)

**Question mode (default):** No LLM calls. Each candidate node is scored by ChromaDB
cosine distance to the query. Nodes sorted by score, top-N selected.

**PR analysis mode:** Calls GPT-4o-mini for each node in a `ThreadPoolExecutor` — parallel
scoring with LLM judgment. Used when analysing pull request impact.

### Step 5: Reduce Step (`reasoning/reduce_step.py`)

**One GPT-4o call.** Receives the full evidence packet:
- `code_snippets`: actual Java source read from `mirror/` by `CodeFetcher`
- `grep_evidence`: raw file:line hits from Route A
- `community_summaries`: plain-English cluster descriptions
- `primary_targets`: node FQNs and file paths from graph traversal
- `property_evidence`: property reader/writer counts and FQNs

Token budget allocation:
```
max_context_tokens    = 32,000  (default; gpt-4o supports up to 128k)
- SYSTEM_TOKENS       =    200
- OUTPUT_RESERVE      =  6,000  (safety/capability/impact intents)
  or NARRATIVE_RESERVE = 20,000  (narrative/explain intents)
= DATA_BUDGET         = 25,800  tokens for evidence

  SNIPPET_BUDGET    = 65% = ~16,770 tokens  (actual code)
  SUMMARY_BUDGET    = 30% =  ~7,740 tokens  (community context)
  EVIDENCE_BUDGET   =  5% =  ~1,290 tokens  (grep file:line list)
```

---

## 8. LLM Layer — Prompts, Intents, and Answer Templates

The Reduce step selects one of 8 system-prompt + template combinations based on query intent.

### Intent → Template Mapping

| Intent | System prompt | Template | Output token reserve |
|---|---|---|---|
| `safety` | `SYSTEM_SAFETY` | `TEMPLATE_SAFETY` | 6,000 |
| `impact` | `SYSTEM_IMPACT` | `TEMPLATE_IMPACT` | 6,000 |
| `code` | `SYSTEM_GENERAL` | `TEMPLATE_CODE` | 6,000 |
| `capability` | `SYSTEM_CAPABILITY` | `TEMPLATE_CAPABILITY` | 6,000 |
| `narrative` | `SYSTEM_NARRATIVE` | `TEMPLATE_NARRATIVE` | **20,000** |
| `general` | `SYSTEM_GENERAL` | `TEMPLATE_GENERAL` | 6,000 |
| `global` | `SYSTEM_GLOBAL` | `TEMPLATE_GLOBAL` | 6,000 |
| `/explain` command | `SYSTEM_EXPLAIN` | `TEMPLATE_EXPLAIN` | 6,000 |

Narrative queries get a 20,000-token output reserve because the LLM uses ~8–12k tokens
internally for reasoning before producing the final story.

### Safety Intent — Critical Verdict Logic

The safety template enforces a **two-step verification** to prevent false YES verdicts:

**STEP 1 — Cross-check graph vs grep for reads:**
Before trusting `"Property readers (graph): NONE"`, scan the grep evidence for any lines
containing `.get(CONSTANT)`, `.containsKey(CONSTANT)`, `.getProperty(CONSTANT)`,
`.getParameter(CONSTANT)`, or extended attribute access with this key.
The property graph tracks only direct `OAuthTokenReqMessageContext` method calls — it does NOT
capture `map.get()`, chained calls like `getProperties().get(KEY)`, or extended attributes.
If ANY such lines appear in grep, treat the constant as **actively read**.

**STEP 2 — Verdict rules:**
- Zero production callers in grep AND 0 property readers AND no read-like grep lines → **YES**
- Production usages exist → **NO** or **YES WITH CHANGES** (list every call site)
- Never say YES solely because the graph shows 0 readers if grep shows read-like patterns

**Answer structure required:**
1. Verdict (YES / YES WITH CHANGES / NO)
2. Property access evidence (writer count + reader count from graph)
3. Production callers to change (from grep: file + line + what to change)
4. Test callers (from grep)
5. Minimal change set (exact steps)

### Capability Intent — Anti-Hallucination Rules

The capability template enforces reasoning rules to prevent the LLM from assuming features
exist just because the data type could theoretically hold them:

- **Type ≠ Behavior:** `List<String>` does NOT prove multi-value support — find where the list is populated
- **Same name ≠ Same thing:** `audience` in JWT claims ≠ `audience` as an HTTP parameter
- **Construction depth:** `Collections.singletonMap(K, V)` creates exactly ONE entry — not recursive
- **Fixed code = fixed behavior:** No loops + no recursion = fixed output structure
- **Absence is evidence:** No `split()`, no loop, no multi-value iteration → capability NOT supported

**Verdict structure required:**
STEP 1: Identify relevant methods
STEP 2: Trace data flow (source → parse → structure → output)
STEP 3: Apply anti-patterns
STEP 4: Bold verdict — YES / NO / CANNOT DETERMINE with exact class+method+line citation

### Regex Fallbacks for Intent Detection

If LLM parser fails, `detect_query_intent()` uses compiled regexes:

| Regex | Matches |
|---|---|
| `_SAFETY_RE` | "is it safe to remove", "can X be removed", "safe to delete", "dead code", "removable" |
| `_CODE_RE` | "show me", "how is X implemented", "how to implement", "what files should I change" |
| `_CAPABILITY_RE` | "does this support", "can it handle multiple", "is X allowed" |
| `_IMPACT_KEYWORDS` | "blast radius", "what breaks", "what calls", "impact", "depends" |
| `_NARRATIVE_RE` | "walk me through", "how does X work", "explain the flow", "step by step" |

Priority order: code → safety → capability → impact → narrative → general.

---

## 9. Keeping the Knowledge Base Current

### Scenario 1: New Repo Added

```bash
# 1. Add repo to manifest
# Edit sample_repos/repos.yaml

# 2. Run full ingest (SQLite export runs automatically at end)
py main.py ingest

# 3. Restart API to load new SQLite snapshot
docker-compose restart nexus-api
```

### Scenario 2: Code Changed in Existing Repo

Same as new repo — re-run full ingest:

```bash
py main.py ingest
docker-compose restart nexus-api
```

The pipeline re-clones/pulls, re-parses changed files, merges into Neo4j via `MERGE`
(idempotent), and produces a fresh SQLite snapshot.

### Scenario 3: SQLite Refresh Only

Neo4j already has the latest graph (e.g. after a manual Cypher edit or after running
a single pipeline stage manually). Re-export without re-parsing:

```bash
py -c "from graph.sqlite_exporter import run_export; run_export()"
docker-compose restart nexus-api
```

Completes in **~5 seconds**.

### Scenario 4: Re-embed After Model Change

If `EMBEDDING_MODEL` changes or you switch to FastEmbed:

```bash
# Delete existing collections (vectors will be re-created on next ingest)
py -c "
import chromadb
from config.settings import settings
c = chromadb.HttpClient(host=settings.chroma_host, port=settings.chroma_port)
c.delete_collection('code_logic')
c.delete_collection('code_intent')
print('deleted — re-run ingest to re-embed')
"

py main.py ingest
```

### Scenario 5: Backfill Property Edges

After adding new method names to `_PROPERTY_READ_METHODS` or `_PROPERTY_WRITE_METHODS`
in `parsers/java_parser.py`, re-parse property edges without full re-ingest:

```bash
py tools/backfill_property_edges.py
py -c "from graph.sqlite_exporter import run_export; run_export()"
docker-compose restart nexus-api
```

### Update Frequency Recommendations

| Situation | Action |
|---|---|
| Active development (daily PRs) | Re-ingest weekly, or use `pipeline/incremental.py` for PR-level analysis |
| Stable codebase | Re-ingest monthly |
| New repo onboarded | Re-ingest immediately |
| Wrong verdict given | SQLite export + restart (if graph is correct); otherwise full re-ingest |

---

## 10. Memory and Scaling

### The RAM Reduction Journey: From ~64 GB to ~4 GB

This section documents every architectural decision that drove the RAM reduction for 100 repos,
in chronological order. Each step is a discrete engineering change with a measured impact.

---

#### Era 1 — Original Architecture (~64 GB required for 100 repos)

The original system queried Neo4j live on every API request. Neo4j runs on the JVM and loads
the entire active graph into its heap to serve low-latency Cypher queries.

**RAM breakdown at 100 repos:**

| Component | RAM |
|---|---|
| Neo4j JVM heap (live graph queries) | ~20–32 GB |
| Neo4j page cache (OS buffer) | ~8–12 GB |
| ChromaDB (float32 embeddings, 100 repos) | ~4.5 GB |
| nexus-api (Python + model loading) | ~3–4 GB |
| nexus-redis | ~50 MB |
| **Total** | **~36–52 GB** |

In practice, a full WSO2 Identity Server analysis required a dedicated 64 GB server.
Running this on a developer workstation (8–16 GB) was impossible.

**Root cause:** Neo4j is designed for OLTP graph workloads — low-latency, random-access queries
on a live mutable graph. This is powerful but requires keeping the entire active dataset in JVM
heap. For a read-only analytical workload (which code analysis is), this is pure overhead.

---

#### Era 2 — Skinny Graph (~20–32 GB for 100 repos)

The first optimization was moving large text blobs out of Neo4j and into ChromaDB.

**Change:** Method body text, Javadoc, and community summaries were removed from Neo4j nodes.
Neo4j now only stores **structural metadata** (FQNs, file paths, line numbers, community IDs,
edge relationships) — no text content.

**Impact:**
- Neo4j data volume dropped ~60%
- Page cache requirement dropped proportionally
- Neo4j heap reduced from ~20–32 GB → ~8–12 GB

**What stayed in Neo4j:** Every node and edge relationship still lived in Neo4j. The live API
still queried Neo4j on every request for graph traversal (blast radius, callers, callees).
This meant Neo4j still had to be running 24/7 and sized for query traffic.

**Remaining bottleneck:** Neo4j JVM overhead is fixed regardless of data volume — even a
small graph requires ~4–8 GB heap minimum for production query throughput.

---

#### Era 3 — SQLite Migration (~3.7 GB for 100 repos) ← Current

This is the decisive architectural shift. Instead of querying Neo4j at runtime, the system
**exports the entire graph to SQLite** after each ingest and reads from SQLite at query time.

**The key insight:** Code analysis is a **read-only, batch-update workload**. The graph only
changes when a new ingest runs (hours-long process). Between ingests, the graph is completely
static. Neo4j's live-graph capabilities (ACID transactions, concurrent writes, real-time updates)
provide zero value for a static read workload — but cost 4–8 GB of JVM heap.

**Changes made:**

1. **`graph/sqlite_exporter.py`** (new): Exports Neo4j → SQLite at end of every ingest.
   The entire graph snapshot is written in ~5 seconds. SQLite is a single file.

2. **`graph/sqlite_retriever.py`** (new): Drop-in replacement for `GraphRetriever`.
   All graph queries (`find_nodes`, `compute_blast_radius`, `get_callers`, `get_callees`,
   `find_property_readers`, `hybrid_search`) go to SQLite + ChromaDB. No Neo4j import.

3. **`api/app.py`**: Removed `GraphDatabase` driver. `SqliteRetriever` injected at startup.

4. **`reasoning/router.py`**: Swapped `GraphRetriever` → `SqliteRetriever`.

5. **`chat.py`**: `_run_explain`, `_run_stats`, cleanup block — all Neo4j references removed.

**The CALLS edge trick:** All CALLS edges are loaded into two Python `defaultdict` objects at
API startup. BFS blast radius becomes pure Python dictionary lookups — O(1) per hop, no I/O.

```python
_callers: dict[str, list[str]]  # callee → [who calls it]
_callees: dict[str, list[str]]  # caller → [what it calls]
```

At 100 repos: ~58,000 CALLS edges × ~300 bytes = **~17 MB** in memory.
The equivalent Neo4j APOC traversal needed the full JVM heap to stay warm.

**RAM impact at 100 repos:**

| Component | Before (Neo4j live) | After (SQLite) | Saving |
|---|---|---|---|
| Graph store | ~8–16 GB (Neo4j JVM) | ~20–50 MB (SQLite hot pages) | **~8–16 GB** |
| CALLS edge BFS | Included in Neo4j heap | ~17 MB (Python dicts) | included above |
| nexus-api overhead | ~3–4 GB (model + Neo4j client) | ~560–700 MB | **~2–3 GB** |
| ChromaDB | ~4.5 GB | ~3.0 GB (same, but int8 pending) | — |
| **Total** | **~20–32 GB** | **~3.7 GB** | **~16–28 GB saved** |

**Neo4j is now completely off during query time.** Stop it after ingest with:
```bash
docker-compose stop nexus-neo4j
```
It only needs to restart when running `py main.py ingest`.

**Query latency impact (bonus):** Removing the Neo4j round-trip also made queries faster.
Neo4j Cypher traversal adds 200–2,000ms per query depending on graph size. SQLite + in-memory
BFS adds ~0ms. Observed query latency dropped by 3–8 seconds.

---

#### Era 4 — int8 Quantization (~1.5 GB for 100 repos, future)

The remaining large consumer is ChromaDB: float32 vectors at 384 dimensions × 100 repos ≈ 3 GB.

**Change:** Set `hnsw:quantization_type: "int8"` on both `code_logic` and `code_intent` collections.
This stores each dimension as a 1-byte int8 instead of a 4-byte float32.

**Impact:** ChromaDB RAM drops from ~3.0 GB → ~0.75 GB for 100 repos. Quality loss < 2%
(int8 quantization of 384-dim cosine vectors is well-studied and robust).

**Requires:** ChromaDB ≥ 0.6.0 (current installed version < 0.6 — check `pip show chromadb`).
After upgrading, existing collections must be deleted and re-embedded for quantization to apply.

**RAM at 100 repos with int8:**

| Component | RAM |
|---|---|
| nexus-api | ~700 MB |
| nexus-chromadb (int8) | ~750 MB |
| nexus-redis | ~10 MB |
| **Total** | **~1.5 GB** |

This would fit 100 repos on a machine with 2 GB available to Docker — a standard developer laptop.

---

#### Summary: The Complete Journey

| Era | Architecture | RAM (100 repos) | What changed |
|---|---|---|---|
| **1** | Neo4j live + float32 ChromaDB | ~36–64 GB | Original design |
| **2** | Skinny Graph (text in ChromaDB) | ~20–32 GB | Moved text blobs to ChromaDB |
| **3** | SQLite Migration ← **current** | **~3.7 GB** | Neo4j offline, SQLite + in-memory BFS |
| **4** | SQLite + int8 quantization ← future | **~1.5 GB** | ChromaDB vector compression |

The SQLite migration (Era 3) delivered the largest single reduction: **~16–28 GB saved**,
by eliminating the JVM heap entirely from the query path. This was a pure architectural
improvement with no quality loss — the same data, stored differently.

---

### RAM Profile

| Component | 10 repos | 100 repos | Scales with |
|---|---|---|---|
| nexus-api (Python + SQLite) | ~560 MB | ~700 MB | Slowly (CALLS edge dict: ~50–80 MB at 100 repos) |
| nexus-chromadb | ~310 MB | ~3.0 GB | ~30 MB per repo (float32 vectors) |
| nexus-redis | ~3 MB | ~10 MB | Negligible |
| nexus-neo4j | ~1–3 GB | ~5–8 GB | **Ingest-only** — stop after ingest |
| **Total (live API, no Neo4j)** | **~870 MB** | **~3.7 GB** | |

100 repos fits comfortably in 8 GB RAM.

### Why RAM Is Low (SQLite vs. Neo4j)

| | Old (Neo4j live) | New (SQLite) |
|---|---|---|
| Graph store RAM | 4–8 GB (JVM heap) | ~20–50 MB hot pages |
| CALLS edge BFS | Cypher APOC traversal (~200ms/hop) | Python dict lookup O(1)/hop |
| Query latency | +500–2000ms (Neo4j overhead) | +0ms (in-memory) |
| Can stop after ingest? | No | **Yes** |

### Scaling Beyond 100 Repos

| Repos | RAM needed | Required action |
|---|---|---|
| Up to 100 | 4 GB | Current config, nothing needed |
| 100–300 | 8–16 GB | Increase Docker Desktop memory limit (Settings → Resources) |
| 300+ | 16–32 GB | Consider int8 quantization (ChromaDB ≥ 0.6) — cuts vector RAM 4× |

**int8 quantization** (when ChromaDB ≥ 0.6 is available):
```python
metadata={"hnsw:space": "cosine", "hnsw:quantization_type": "int8"}
```
Cuts ChromaDB from ~30 MB/repo → ~7.5 MB/repo. Requires delete + re-embed. <2% quality loss.

---

## 11. Monitoring and Observability

### Health Checks

```bash
# API
curl http://localhost:8080/health

# ChromaDB
curl http://localhost:8000/api/v1/heartbeat

# Redis
docker exec nexus-redis redis-cli ping    # → PONG

# SQLite — verify data is present and current
py -c "
from config.settings import settings
import sqlite3, os, datetime
db = str(settings.sqlite_db_path)
mtime = datetime.datetime.fromtimestamp(os.path.getmtime(db))
with sqlite3.connect(db) as c:
    n = c.execute('SELECT count(*) FROM nodes').fetchone()[0]
    e = c.execute('SELECT count(*) FROM calls_edges').fetchone()[0]
    p = c.execute('SELECT count(*) FROM property_edges').fetchone()[0]
print(f'nodes={n}  calls={e}  props={p}  last_modified={mtime}')
"
```

### Pipeline Stage (During Ingest)

```bash
# Current stage
docker exec nexus-redis redis-cli get nexus:pipeline:stage

# Watch live (Linux/Mac)
watch -n5 "docker exec nexus-redis redis-cli get nexus:pipeline:stage"

# Watch live (Windows PowerShell)
while ($true) { docker exec nexus-redis redis-cli get nexus:pipeline:stage; Start-Sleep 5 }
```

### Docker Stats

```bash
docker stats --no-stream
```

| Container | Alert if |
|---|---|
| `nexus-api` | Memory > 1.5 GB (restart container) |
| `nexus-chromadb` | Memory > 80% of container limit → reduce `HYBRID_SEARCH_N_RESULTS` |

### Log Levels

```bash
# View live API logs
docker-compose logs -f nexus-api

# Last 100 lines
docker-compose logs --tail=100 nexus-api

# Debug mode (set in .env)
LOG_LEVEL=DEBUG
```

### Key Metrics

| Metric | Location | Alert threshold |
|---|---|---|
| Query latency | API logs `Latency: Xms` | > 60s consistently |
| ChromaDB RAM | `docker stats` | > 80% of limit |
| SQLite file age | `ls -la data/nexus_graph.db` | Older than last expected ingest |
| Missing collections | `curl localhost:8000/api/v1/collections` | `code_logic` or `code_intent` absent |
| Redis pipeline stage stuck | `redis-cli get nexus:pipeline:stage` | Same stage for > 2h |

---

## 12. Troubleshooting

### API returns empty answers or "not found in graph"

**Cause:** SQLite file missing, empty, or stale.

```bash
# Check file
ls -la nexus/data/nexus_graph.db
py -c "import sqlite3; c=sqlite3.connect('data/nexus_graph.db'); print(c.execute('SELECT count(*) FROM nodes').fetchone())"
```

**Fix:**
```bash
py -c "from graph.sqlite_exporter import run_export; run_export()"
docker-compose restart nexus-api
```

---

### `Route 'grep' failed` in chat output

**Cause:** Exception in grep/symbolic route. Fallback to semantic occurred.

Common causes:
- `rg` (ripgrep) not installed → set `GREP_BACKEND=python` in `.env`
- SQLite query error in blast radius → check logs for SQL error
- FQN not found in `nodes` table → run SQLite export, restart API

---

### `chromadb.errors.InvalidArgumentError: unknown field hnsw:quantization_type`

**Cause:** ChromaDB version < 0.6 does not support int8 quantization.

**Fix:** Remove `hnsw:quantization_type` from collection metadata — already done in current code.
If still seeing this, ensure you are on the latest code version.

---

### `SqliteRetriever.get_community_summaries_by_ids() takes 3 positional arguments but 4 were given`

**Cause:** Signature mismatch — `_chroma_exec()` in `router.py` always prepends `chroma_client`
as the first argument. The `SqliteRetriever` method must accept it.

**Fix:** Ensure the method signature is:
```python
def get_community_summaries_by_ids(self, chroma_client, community_ids, collection_name="community_summaries"):
```

---

### Safety verdict says YES but constant IS used

**Cause:** Property graph captured 0 readers but grep evidence shows `.get(KEY)`, `.containsKey(KEY)`,
or chained calls like `getProperties().get(KEY)` — these are not captured by the property edge parser.

**Fix applied:** `TEMPLATE_SAFETY` STEP 1 now requires the LLM to scan grep evidence for
read-like patterns before trusting the graph's "0 readers" count.
Additionally, `parsers/java_parser.py` `_PROPERTY_READ_METHODS` now includes `containsKey`,
`remove`, `getExtendedAttribute`, `getParameters`.

If the problem recurs: run `py tools/backfill_property_edges.py`, then SQLite export + restart.

---

### Neo4j connection refused during ingest

**Cause:** Neo4j container not running or still starting (~60s startup time).

```bash
docker-compose ps nexus-neo4j
docker-compose logs --tail=50 nexus-neo4j
docker-compose start nexus-neo4j
# Wait 60 seconds, then retry ingest
```

---

### ChromaDB upsert fails with `Connection aborted` / `WinError 10053`

**Cause:** HTTP payload too large (very long method bodies exceed ChromaDB buffer).

**Fix:** Reduce `EMBEDDING_BATCH_SIZE` in `.env` (try 50, then 25).
The embedder already splits batches in half on connection errors with exponential backoff
(1s → 2s → 4s → 8s cap) — this is the automatic fallback.

---

### Community summaries are empty after ingest

**Cause:** Leiden detected no communities (graph too sparse, missing relationship types).

**Diagnosis:**
```bash
# Check if CALLS edges exist in Neo4j
docker exec nexus-neo4j cypher-shell -u neo4j -p nexuspassword \
  "MATCH ()-[:CALLS]->() RETURN count(*) AS c"
```

**Fix:**
1. Verify `OSGI_ENABLED=true` in `.env` (adds RESOLVES_TO edges which help Leiden)
2. Increase `LEIDEN_GAMMA` to produce more communities
3. If CALLS count is 0: re-run ingest from the load stage

---

### Memory pressure during ingest

**Tune these in order of impact:**
1. `MAX_CONCURRENT_REPOS=2` (fewer parallel repo processes)
2. `PARSER_FILE_BATCH_SIZE=100` (fewer files per worker)
3. `EMBEDDING_BATCH_SIZE=50` (smaller ChromaDB batches)
4. Increase Docker Desktop RAM: Settings → Resources → Memory

---

## 13. Security

### API Authentication

By default, the API has no authentication (development mode). For production:

```env
API_KEYS=key1,key2,key3
```

Clients send: `Authorization: Bearer key1`

Empty `API_KEYS` (default) disables auth — do not expose publicly without setting this.

### Secrets

- `.env` is in `.gitignore` — never commit it
- `LLM_API_KEY` is the only secret sent outside the machine (to OpenAI/Azure)
- Neo4j password is internal (Docker network only)
- ChromaDB has no built-in auth — bind to `127.0.0.1` in production

### Network Hardening

Restrict Docker port bindings for production:

```yaml
# docker-compose.yml
services:
  nexus-api:
    ports:
      - "127.0.0.1:8080:8080"
  nexus-chromadb:
    ports:
      - "127.0.0.1:8000:8000"
  nexus-redis:
    ports:
      - "127.0.0.1:6379:6379"
```

---

## 14. Backup and Recovery

### What to Back Up

| Item | Path | Frequency | Priority |
|---|---|---|---|
| SQLite graph snapshot | `data/nexus_graph.db` | After every ingest | **Critical** |
| `.env` | Project root | On change | **Critical** |
| ChromaDB volume | Docker volume `nexus_chroma_data` | After every ingest | High |
| `repos.yaml` | `sample_repos/repos.yaml` | On change | Medium |
| Neo4j volume | Docker volume `nexus_neo4j_data` | After every ingest | Low (rebuildable) |

### Backup Commands

```bash
# SQLite (instant file copy)
cp data/nexus_graph.db data/nexus_graph.db.bak

# ChromaDB volume (Linux/Mac)
docker run --rm \
  -v nexus_chroma_data:/data \
  -v $(pwd)/backups:/backup \
  alpine tar czf /backup/chroma_$(date +%Y%m%d).tar.gz /data

# ChromaDB volume (Windows PowerShell)
$date = Get-Date -Format "yyyyMMdd"
docker run --rm -v nexus_chroma_data:/data -v ${PWD}/backups:/backup `
  alpine tar czf /backup/chroma_$date.tar.gz /data
```

### Recovery Scenarios

**SQLite corrupted or deleted (Neo4j still has graph):**
```bash
py -c "from graph.sqlite_exporter import run_export; run_export()"
docker-compose restart nexus-api
# Recovery time: ~5 seconds
```

**SQLite corrupted + Neo4j data gone:**
```bash
docker-compose start nexus-neo4j
py main.py ingest
# Recovery time: full pipeline (~4h for 10 repos)
```

**ChromaDB data lost:**
```bash
# Option 1: Restore from backup
docker run --rm -v nexus_chroma_data:/data -v $(pwd)/backups:/backup \
  alpine tar xzf /backup/chroma_YYYYMMDD.tar.gz -C /

# Option 2: Re-embed from scratch (re-run ingest)
py main.py ingest
```

---

## 15. Component Reference

### Key Files

| File | Role |
|---|---|
| `main.py` | CLI entry point: `py main.py ingest`, `py main.py query "..."` |
| `chat.py` | Interactive REPL for development/testing |
| `api/app.py` | FastAPI REST server. No Neo4j imports. Uses `SqliteRetriever`. |
| `config/settings.py` | All configuration. `make_llm_client(tier)` factory for two-tier strategy. |
| `pipeline/orchestrator.py` | Runs all 16 ingest stages. Adds SQLite export as final step. |
| `graph/sqlite_exporter.py` | Exports Neo4j → SQLite. Standalone: `py -c "from graph.sqlite_exporter import run_export; run_export()"` |
| `graph/sqlite_retriever.py` | Live API graph queries. Loads CALLS edges at startup. No Neo4j. |
| `reasoning/router.py` | Classifies questions into 4 routes + 6 intents. `LLMQueryParser` → `QueryClassifier` → route execution. |
| `reasoning/reduce_step.py` | 8 system prompts + 8 templates. One GPT-4o call per query. |
| `reasoning/map_step.py` | Scores candidates. Zero LLM calls in question mode. |
| `parsers/java_parser.py` | Tree-sitter Java AST parser. Extracts UIR objects. |
| `parsers/rfc_semantic_matcher.py` | Two-stage RFC→code mapper. ChromaDB + GPT-4o-mini verification. |
| `vectorstore/embedder.py` | ChromaDB embedder. `ChromaEmbedder` (MiniLM) or `FastEmbedder` (Nomic/GPU). |
| `community/summarizer.py` | Leiden community → GPT-4o-mini summary → ChromaDB. |
| `community/global_rollup.py` | L1→L2→L3 GraphRAG rollup. |
| `tools/backfill_property_edges.py` | Re-parses READS/WRITES_PROPERTY edges without full re-ingest. |

### API Endpoints

| Method | Path | Description |
|---|---|---|
| `GET` | `/health` | Liveness check |
| `POST` | `/query` | Ask a question: `{"question": "..."}` |
| `POST` | `/query/stream` | Streaming SSE response |
| `GET` | `/stats` | SQLite + ChromaDB counts |
| `GET` | `/explain/{fqn}` | Explain a class or method |

### Chat CLI Commands

```
/explain <fqn>    — callers, callees, LLM explanation of a specific node
/stats            — graph node counts, ChromaDB collection sizes
/help             — available commands
exit / quit       — exit
```

### Environment Variables — Complete Reference

```env
# ── Required ──────────────────────────────────────────────────
LLM_API_KEY=sk-...

# ── LLM Models ────────────────────────────────────────────────
LLM_PROVIDER=openai             # openai | azure
LLM_FAST_MODEL=gpt-4o-mini     # bulk tasks (ingest + map scoring)
LLM_STRONG_MODEL=gpt-4o        # final reduce answer
LLM_PARSER_MODEL=gpt-4o-mini   # query intent + symbol extraction

# ── Azure-specific (if LLM_PROVIDER=azure) ───────────────────
LLM_AZURE_ENDPOINT=https://<name>.openai.azure.com
LLM_AZURE_API_VERSION=2025-04-01-preview
LLM_DEPLOYMENT=gpt-4o-mini
LLM_QUERY_DEPLOYMENT=gpt-4o
LLM_PARSER_DEPLOYMENT=gpt-4o-mini

# ── Storage ───────────────────────────────────────────────────
SQLITE_DB_PATH=./data/nexus_graph.db
REPOS_MIRROR_PATH=./mirror
REPOS_CONFIG_PATH=./sample_repos/repos.yaml

# ── ChromaDB ──────────────────────────────────────────────────
CHROMA_HOST=localhost
CHROMA_PORT=8000

# ── Neo4j (ingest only — not used by live API) ────────────────
NEO4J_URI=bolt://localhost:7687
NEO4J_USER=neo4j
NEO4J_PASSWORD=nexuspassword

# ── Performance ───────────────────────────────────────────────
MAX_CONCURRENT_REPOS=5          # lower if OOM during ingest
PARSER_FILE_BATCH_SIZE=200      # files per parse worker
EMBEDDING_BATCH_SIZE=100        # chunks per ChromaDB upsert
SUMMARIZER_MAX_WORKERS=4        # parallel community summarization
HYBRID_SEARCH_N_RESULTS=20      # candidates per route
BLAST_RADIUS_DEPTH=3            # BFS hops for impact analysis
CHUNK_SIZE=512                  # max tokens per code chunk
CHUNK_OVERLAP=128               # sliding window overlap
LEIDEN_GAMMA=1.5                # community granularity (higher = smaller communities)
MAX_CONTEXT_TOKENS=32000        # LLM context window

# ── Optional features ─────────────────────────────────────────
USE_FASTEMBED=false             # GPU-accelerated Nomic embed
FASTEMBED_MODEL=nomic-ai/nomic-embed-text-v1.5
LOCAL_DRAFTING_ENABLED=false    # Ollama micro-drafts for EntryPoints
OLLAMA_BASE_URL=http://localhost:11434
OLLAMA_MODEL=llama3.2:3b
OSGI_ENABLED=true               # parse @Component/@Reference → RESOLVES_TO edges
RFC_PATH=./rfcs                 # directory of RFC .txt files

# ── API ───────────────────────────────────────────────────────
API_KEYS=                       # empty = no auth; comma-separated = Bearer token auth
API_PORT=8080
LOG_LEVEL=INFO                  # INFO | DEBUG | WARNING
```
