# CodeNexus — Beginner's Guide

> **Who this is for:** Someone who has never worked on this project before and just
> wants to understand what it is, what every piece of technology does, and what
> each file is responsible for. No prior knowledge of graph databases, vector
> search, or LLMs is assumed.

---

## Table of Contents

1. [What is CodeNexus in plain English?](#1-what-is-codenexus-in-plain-english)
2. [The Big Picture — How the pieces fit together](#2-the-big-picture)
3. [Technology Glossary — Every tool explained simply](#3-technology-glossary)
4. [File-by-File Reference](#4-file-by-file-reference)
5. [How to Run It](#5-how-to-run-it)
6. [Common Questions](#6-common-questions)
7. [Reading the Ingest Log — What Every Line Means](#7-reading-the-ingest-log)

---

## 1. What is CodeNexus in Plain English?

Imagine you have **100+ Java repositories** — millions of lines of code — and you
want to ask questions like:

- *"Does this system support multiple audience values in a token exchange request?"*
- *"What other code would break if I changed this method?"*
- *"How does the OAuth token refresh flow work across services?"*

You could try reading the code yourself, but that would take weeks. CodeNexus
**automatically reads all the code**, builds a searchable map of it, and lets you
ask questions in plain English.

It does this in two phases:

| Phase | What happens | When it runs |
|---|---|---|
| **Ingestion** | Reads all Java files, builds the map, stores it | Once (or after code changes) |
| **Query** | Takes your question, finds the relevant code, answers it | Every time you ask |

The "map" it builds has **two completely separate parts** that serve different purposes:

| Store | Technology | What it holds | What it answers |
|---|---|---|---|
| **Graph database** | Neo4j | *Relationships* — who calls who, which class implements which interface, which module depends on which module | "What would break if I changed this?" |
| **Vector database** | ChromaDB | *Meaning* — each method converted to numbers so you can search by concept | "What code is related to token validation?" |

> **Important:** These are two independent databases. Neo4j is NOT converted into ChromaDB or vice-versa.
> They are populated in parallel from the same parsed Java code. During a query, both are searched
> and their results are combined before being handed to the LLM.

---

## 2. The Big Picture

### Ingestion (building the knowledge base)

```
Your Java repos on GitHub / GitLab
            │
            │  git clone
            ▼
     ./mirror/  (local copy of all repos)
            │
            │  ProcessPoolExecutor: Tree-sitter reads every .java file
            │  across all CPU cores in parallel and extracts:
            │  - class names, method names
            │  - what each method calls
            │  - Javadoc comments
            │  - annotations (@Override, @Autowired, @Value, etc.)
            │  - method visibility (public/private/protected)
            │  - modifiers (static, abstract, final, synchronized)
            │  - OSGi lifecycle roles (@Activate, @Deactivate)
            │  - JAX-RS paths merged (class @Path + method @Path)
            ▼
    UIR Objects  (Python data structures representing
                  every class and method found)
            │
            │  AST-aware sliding window chunker:
            │  Splits long methods into 512-token windows
            │  with 128-token overlap. Each chunk prefixed
            │  with [Package: …] [Class: …] for context.
            │
       ┌────┴────┐
       │         │
       ▼         ▼
    Neo4j     ChromaDB
  (graph:     (vectors:
  who calls    what does
  who, which   each method
  class uses   "mean"?)
  which class)
       │
       ▼
  OSGi Resolution
  (@Component → @Reference bindings via [:RESOLVES_TO] edges)
       │
       ▼
  RFC Specification Grounding
  (RFC markdown files → (:Specification) nodes;
   "// RFC 6749" citations in Java → [:IMPLEMENTS_SPEC] edges)
       │
       ▼
  Ollama Local Micro-Drafts (optional)
  (3-sentence summaries stored on EntryPoint Component nodes
   at $0 cost using llama3.2 running locally)
       │
       ▼
  Leiden Algorithm
  (groups related code into "communities" —
   like clustering related topics together)
       │
       ▼
  GPT-4o-mini writes a plain-English summary
  of each community → stored in ChromaDB
       │
       ▼
  NodeTagger labels API endpoints as :EntryPoint
  and database classes as :DataSink
       │
       ▼
  GDS Dijkstra finds shortest paths from
  each EntryPoint to each DataSink
  (CALLS=1 weight, REMOTE_CALLS=5 cross-service penalty)
       │
       ▼
  GPT-4o-mini writes an "end-to-end story"
  for each execution path → stored in ChromaDB
       │
       ▼
  Global Rollup groups community summaries into
  sub-system summaries (L2) and a single master
  Global Architecture Document (L3)
```

### Query (answering your question)

```
Your question: "does this support nested act claims?"
            │
            │  Router classifies the question
            ▼
  ┌─────────────────────────────┐
  │  What kind of question?     │
  └──┬──────┬──────┬──────┬────┘
     │GLOBAL │SYMBOL│ NAME │ GENERAL
     ▼       ▼      ▼      ▼
  Return the Search  Search Search
  Global    files   Neo4j  ChromaDB
  Arch Doc  (grep)  by     by
  directly          exact  meaning
                    name
     │       │      │      │
     ▼       └──┬───┘      │
  (done!)       │          │
                ▼          │
         Expand through     │
         the call graph     │
                │           │
                └─────┬────┘
                      ▼
              Assemble code snippets
              + community summaries
                      │
                      ▼
              GPT-4o (strong model) reads
              all evidence and writes the answer
```

---

## 3. Technology Glossary

### Python
The programming language the entire CodeNexus application is written in.
Version 3.11+ is required (3.11+ needed for `tomllib` stdlib; 3.14 is tested and supported).

On Windows, Python is invoked as `py` (the Python Launcher), not `python`.

---

### Neo4j
**What it is:** A *graph database* — instead of storing data in spreadsheet-style
tables (like MySQL), it stores data as *nodes* (things) and *relationships* (links
between things).

**Why we use it:** Code is naturally a graph. Class A *calls* method B.
Class C *implements* interface D. Class E *depends on* module F. These
relationships are exactly what Neo4j is designed to store and query efficiently.

**What we store in it:**
- **Nodes:** Every Java project, Maven module, Java class, and Java method
  (each with visibility, modifier flags, annotations as JSON, community ID,
   optional `micro_draft` property on EntryPoint components)
- **Edges:** 19 relationship types — CALLS, IMPLEMENTS, EXTENDS, DEPENDS_ON,
  INJECTS, ANNOTATED_WITH, THROWS, OVERRIDES, INSTANTIATES, RETURNS,
  RECEIVES, HANDLES_EVENT, REMOTE_CALLS, CONTAINS, QUERIES_TABLE, READS_CONFIG,
  RESOLVES_TO, IMPLEMENTS_SPEC, DECLARES

**How we talk to it:** Using **Cypher** — Neo4j's query language.
Example: `MATCH (a:LogicUnit)-[:CALLS]->(b:LogicUnit) RETURN a, b`
means "find all methods that call another method".

**Where it runs:** In a Docker container on your machine (port 7687).

---

### ChromaDB
**What it is:** A *vector database* — stores numbers (called "vectors" or
"embeddings") that represent the *meaning* of text, so you can search
by meaning instead of exact words.

**Why we use it:** If you ask "how does token validation work?", exact-text
search won't help because the code says things like `verifyJwtSignature()`,
not "token validation". ChromaDB can find that method because it understands
that "token validation" and "verifyJwtSignature" are semantically similar.

**What we store in it:** Six collections:
- `code_logic` — one entry per chunk: the method's source code as a vector (with context prefix)
- `code_intent` — one entry per method: the method's Javadoc as a vector
- `community_summaries` — plain-English summaries of code clusters
- `flow_narratives` — end-to-end stories describing API-to-database execution paths
- `l2_subsystem_summaries` — sub-system summaries grouping related communities by domain
- `l3_global_architecture` — a single master Global Architecture Document

**How vectors are created:**
For `code_logic` and `code_intent`: a HuggingFace model (`all-MiniLM-L6-v2`, 384-dimensional)
or `nomic-ai/nomic-embed-text-v1.5` via FastEmbed (768-dimensional, GPU-accelerated)
running locally on your machine converts text into a list of numbers.
Similar texts produce similar numbers. **No OpenAI API call needed here.**

For the other collections: GPT-4o-mini writes the text, then ChromaDB converts it to a vector.

**Where it runs:** In a Docker container on your machine (port 8000).

---

### Redis
**What it is:** An in-memory key-value store — like a fast Python dictionary
that lives outside your Python process so it survives restarts.

**Why we use it:** Used to track pipeline stage during ingestion (which stage is
currently running). If the ingestion crashes halfway, Redis allows monitoring of state.

**Where it runs:** In a Docker container on your machine (port 6379).

---

### Docker / Docker Compose
**What it is:** A tool that runs applications in isolated "containers". Each container
has exactly the software it needs.

**Why we use it:** Instead of asking you to install Neo4j, ChromaDB, and Redis
on your machine (complex, version-sensitive), Docker runs them as containers.
One command (`docker compose up -d`) starts all three.

---

### Tree-sitter
**What it is:** A fast, accurate source code parsing library. It reads Java source
code and builds an **Abstract Syntax Tree (AST)** — a structured representation
of the code's grammar.

**Why we use it:** When you read a Java file as plain text, you see characters.
Tree-sitter reads it and understands structure: "this is a class declaration,
this is a method, this method has these parameters and modifiers, it calls these other methods".

**The library used:** `tree-sitter-java` — the Tree-sitter grammar for Java.

**What v2 adds:** v2 Sprint 1 extracts the `modifiers` child node (visibility, static,
abstract, final, synchronized), parses annotation arguments as structured dicts
(not just raw strings), recurses into lambda bodies for call extraction, and
preserves generic types like `List<User>` in type fields.

---

### AST-Aware Sliding Window Chunker
**What it is:** A method-body text splitter that never cuts across function boundaries.
When a method body exceeds 512 tokens, it generates multiple overlapping text windows.

**Why it matters:** Embedding models have a maximum input size. A 2,000-line authentication
method cannot be embedded as one block. The sliding window splits it into 512-token
windows with a 128-token overlap so the model can "see" each portion without losing context.

**Context prefix:** Every chunk begins with `[Package: org.wso2.identity] [Class: AuthzEndpoint]`
so even an isolated code fragment can be matched to its origin — critical for large monorepos
where many methods have the same name in different packages.

**The parameters:**
- `CHUNK_SIZE=512` — maximum tokens per window
- `CHUNK_OVERLAP=128` — token overlap between adjacent windows
- Javadoc (code_intent) is **never** windowed — always stored as one chunk

---

### HuggingFace SentenceTransformers
**What it is:** A Python library that runs pre-trained AI models locally to
convert text into vectors.

**The model we use:** `all-MiniLM-L6-v2` — a small, fast model producing
384-dimension vectors. Runs entirely on CPU, no GPU required, no API key needed.

**What "Batches: 100%" means in the terminal:**
```
Batches: 100%|████████████████| 1/1 [00:00<00:00, 3.48it/s]
```
This is the HuggingFace model processing a group of method texts. "1/1" means one batch.
This is **not** related to Neo4j — it is purely the ChromaDB embedding step.

---

### FastEmbed + ONNX Runtime (GPU-accelerated, optional)
**What it is:** A faster, higher-quality embedding library from Qdrant that can use
your GPU via ONNX Runtime. Produces 768-dimensional vectors using `nomic-ai/nomic-embed-text-v1.5`.

**Why it's better (when available):** The Nomic model produces richer semantic embeddings
(768-dim vs 384-dim) and runs much faster on a CUDA GPU. Useful for large monorepos with
100,000+ methods.

**How to enable it:**
```bash
# CPU-only (slightly richer model than MiniLM):
pip install fastembed onnxruntime

# GPU (CUDA 11.x or 12.x):
pip install fastembed-gpu onnxruntime-gpu

# Enable in .env:
USE_FASTEMBED=true
FASTEMBED_MODEL=nomic-ai/nomic-embed-text-v1.5
```

**Automatic detection:** CodeNexus checks `onnxruntime.get_available_providers()` for
`CUDAExecutionProvider` and falls back to CPU automatically.

---

### ProcessPoolExecutor (Parallel Parsing)
**What it is:** Python's built-in multiprocessing library. Runs multiple Python processes
in parallel across your CPU cores.

**Why we use it:** Tree-sitter parsing and token counting are CPU-bound operations.
On a machine with 8 cores, parsing 8,000 Java files takes ~1/8 the time compared to
running on a single core.

**How it works:**
- Files are split into batches of `PARSER_FILE_BATCH_SIZE=200` files
- Each batch is sent to a separate subprocess that imports Tree-sitter + Chunker independently
- Results (serialized as plain Python dicts) come back to the main process
- Main process handles all Neo4j and ChromaDB writes (no shared database state in workers)

---

### OpenAI GPT Models — Two-Tier Strategy

CodeNexus uses two OpenAI models with different cost/quality trade-offs:

| Model | Used for | Why |
|---|---|---|
| **gpt-4o-mini** (fast) | Community summarisation (bulk), map-step scoring, flow narratives, L2 sub-system rollup | ~90% cheaper than gpt-4o; adequate for bulk tasks |
| **gpt-4o** (strong) | Final reduce answer, L3 global rollup only | Best quality for the answer the user sees |

This means a full ingest of 100 repos costs ~$0.50 instead of ~$5+ if everything used gpt-4o.

**What the LLM does NOT do:** It does not search. It does not access the internet.
It only reads the evidence we hand it and synthesises an answer.

**Key constraint:** Every prompt is capped at **8,000 tokens** to control costs.
The `community/prompt_builder.py` `DATA_BUDGET` constant (6,800 tokens) enforces this.

---

### Ollama (Local LLM Micro-Drafts, optional)
**What it is:** A free tool that runs small open-source LLMs (like Llama 3.2)
entirely on your local machine. No API key, no internet, no cost.

**Why we use it:** Every API endpoint class (`:EntryPoint`) can have a short 3-sentence
"micro-draft" summary stored directly in Neo4j as a `micro_draft` property. Writing
hundreds of these summaries with GPT-4o-mini would cost money. Ollama does it for free.

**How to enable it:**
```bash
# 1. Install Ollama: https://ollama.com/
# 2. Start it:
ollama run llama3.2

# 3. Enable in .env:
LOCAL_DRAFTING_ENABLED=true
OLLAMA_BASE_URL=http://localhost:11434
OLLAMA_MODEL=llama3.2:3b
```

**Graceful degradation:** If Ollama is not running, this stage is silently skipped.
No errors, no impact on the rest of the pipeline.

---

### OSGi Declarative Services
**What it is:** OSGi (Open Service Gateway initiative) is a Java component framework
used by WSO2 Identity Server. Components declare which interfaces they provide
(`@Component(service={Interface.class})`) and which they need (`@Reference Interface field`).

**Why we index it:** OSGi wiring cannot be seen in `new` calls or import statements.
An `@Reference AuthzService authzService` field is resolved at runtime by the OSGi
container. Without special parsing, the graph would have no edge from the caller to
the implementation.

**What CodeNexus does:**
- `parsers/osgi_parser.py` scans all Java files for `@Component` and `@Reference` annotations
- Builds `[:RESOLVES_TO]` edges from each injected interface to its implementation
- These edges appear in the flow graph and are traversed by Dijkstra shortest path

---

### RFC Specification Grounding
**What it is:** IETF RFCs (Request for Comments) are the official specifications for
internet protocols. OAuth 2.0 is RFC 6749. JWT is RFC 7519.

**Why it matters:** When you ask "does this correctly implement RFC 8693 token exchange?",
the system can cross-reference the specification text against the code that cites it.

**How it works:**
- Place RFC markdown files in the `./rfcs/` directory
- `parsers/rfc_parser.py` parses them into `(:Specification)` nodes in Neo4j
- Java source comments like `// RFC 6749`, `// See RFC 7519 Section 4.1` are detected automatically
- `[:IMPLEMENTS_SPEC]` edges link the Java class to the specification node

---

### Leiden Algorithm (via Neo4j GDS)
**What it is:** A community detection algorithm — groups code into clusters
("communities") of closely related code.

**Why we use it:** Instead of asking the LLM to read all 1 million lines of code,
we ask it to read a summary of the relevant cluster.

**Neo4j GDS (Graph Data Science):** The Neo4j plugin that runs Leiden and other
graph algorithms directly inside the database.

**Leiden resolution parameter (`LEIDEN_GAMMA`):** Default 1.5. Higher values = smaller,
more granular communities. Lower values = larger, broader communities.

---

### Weighted GDS Dijkstra
**What it is:** Dijkstra's shortest-path algorithm running inside Neo4j Graph Data Science,
using edge weights to find the most efficient execution path through the code graph.

**Why weights matter:** A CALLS edge (one method calling another in the same service)
costs 1. A REMOTE_CALLS edge (REST call crossing a service boundary) costs 5. This means
Dijkstra prefers paths that stay within a service before hopping to another microservice —
which mirrors how you'd think about code flow when reading it.

**The effect:** Paths are ranked by execution complexity. A short 3-hop path entirely
within one service scores lower (better) than a 3-hop path that crosses two service boundaries.

---

### Pydantic / pydantic-settings
**What it is:** A Python library for data structures with automatic type validation.
`pydantic-settings` reads settings from `.env` files.

**Why we use it:** All configuration lives in a `.env` file and is loaded into a single
`Settings` object. No magic strings scattered through the code.

---

### Tenacity
**What it is:** A Python retry library — if a Neo4j/ChromaDB/LLM call fails due to a
transient error, it automatically retries with increasing delays.

---

### Tiktoken
**What it is:** OpenAI's token counting library — counts how many "tokens" a text
string contains, to stay under the 8K context limit and to measure sliding window
sizes during chunking.

---

### ripgrep (`rg`)
**What it is:** An extremely fast text search tool written in Rust (~10× faster than grep).
Searches through gigabytes of source code in seconds.

**Why we use it:** When the router identifies a specific symbol (like `IMPERSONATED_SUBJECT`),
ripgrep finds every `.java` file containing that exact string, with file path and line number.

---

### tomllib (Python 3.11+ stdlib)
**What it is:** A TOML file parser built into Python 3.11+. No extra package needed.

**Why we use it:** WSO2 Identity Server uses `deployment.toml` for configuration.
`tomllib` parses nested TOML tables accurately (e.g. `[[transport.https]]`) where the
previous regex-based parser would miss or mislabel nested sections.

---

## 4. File-by-File Reference

### Root directory

| File | What it does |
|---|---|
| `main.py` | The main entry point. Run `py main.py ingest` to ingest repos, `py main.py query "..."` to ask a question. |
| `docker-compose.yml` | Defines the three Docker services: `nexus-neo4j`, `nexus-chromadb`, `nexus-redis`. |
| `requirements.txt` | List of all Python packages the project depends on with version constraints. |
| `.env` | Your local config. Contains passwords, API keys, model names, batch sizes. Never commit this file. |
| `.env.example` | A safe template showing all available settings with default values. Copy to `.env`. |
| `.gitignore` | Tells Git which files to ignore (keys, venv, caches, ML weights, Docker data volumes, etc.) |

---

### `config/`

| File | What it does |
|---|---|
| `config/__init__.py` | Makes `config` a Python package. |
| `config/settings.py` | **Central configuration.** Defines a `Settings` class (Pydantic) that reads every setting from `.env`. All other modules import `settings` from here. Contains `make_llm_client(tier)` factory and `get_model_name(tier)` helper for the two-tier LLM strategy. Sprint 1–4 settings added: CHUNK_SIZE, CHUNK_OVERLAP, USE_FASTEMBED, FASTEMBED_MODEL, OSGI_ENABLED, RFC_PATH, LOCAL_DRAFTING_ENABLED, OLLAMA_BASE_URL, OLLAMA_MODEL. |

---

### `parsers/`

| File | What it does |
|---|---|
| `parsers/__init__.py` | Makes `parsers` a Python package. |
| `parsers/uir.py` | **Universal Intermediate Representation.** Defines the Python data classes (`Project`, `Module`, `Component`, `LogicUnit`, `Parameter`, `FieldDeclaration`) that hold all parsed Java information. `LogicUnit` carries `visibility`, `is_static`, `is_abstract`, `is_final`, `is_synchronized`, `lifecycle_role`. All `annotations` fields are `list[dict]` (structured). |
| `parsers/java_parser.py` | **The Java AST parser.** Uses Tree-sitter to extract: class names, method names, Javadoc, method bodies, annotations (as structured dicts), modifiers (visibility/static/abstract), OSGi lifecycle markers, parameter annotations (`@QueryParam`, `@PathVariable`), lambda call bodies, and generic types. |
| `parsers/geid.py` | **Global Entity ID generator.** Creates a 16-character unique identifier: `SHA256(repo_name + "::" + fqn)[:16]`. Shared between Neo4j and ChromaDB. |
| `parsers/fqn_builder.py` | **Fully Qualified Name builder.** Assembles the canonical Java name for a class or method. |
| `parsers/javadoc_parser.py` | Parses Javadoc comment blocks to extract `@param`, `@return`, `@throws`, `@see` tags. |
| `parsers/reflection.py` | Utility for resolving Java type names and handling generic type edge cases. |
| `parsers/sql_schema_parser.py` | **SQL DDL Parser.** Scans `dbscripts/` for `CREATE TABLE` statements → `DatabaseTable` nodes + `QUERIES_TABLE` edges. |
| `parsers/config_parser.py` | **Configuration Parser.** Parses WSO2 `deployment.toml` (using `tomllib` for accurate nested tables), `*.xml`, `*.properties`, and `application.yml`. Creates `Configuration` nodes and `READS_CONFIG` edges. |
| `parsers/osgi_parser.py` | **OSGi Declarative Services Parser (Sprint 2).** Scans Java for `@Component(service={...})` and `@Reference` annotations. Builds `[:RESOLVES_TO]` edges from injected interface fields to their OSGi implementation classes. Resolves service bindings invisible to normal call-graph analysis. |
| `parsers/rfc_parser.py` | **RFC Specification Grounding Parser (Sprint 4).** Parses RFC markdown/text files from `./rfcs/` into `(:Specification)` nodes. Scans Java source for inline RFC citations (e.g. `// RFC 6749`, `// See RFC 7519`) and creates `[:IMPLEMENTS_SPEC]` edges linking classes to the specifications they implement. |

---

### `llm/` (Sprint 4)

| File | What it does |
|---|---|
| `llm/__init__.py` | Makes `llm` a Python package. |
| `llm/local_drafting.py` | **Ollama Local Micro-Draft Engine.** Generates 3-sentence architectural summaries for every `:EntryPoint` class using a local Ollama LLM (default: `llama3.2:3b`). Stores the result as a `micro_draft` property on the Neo4j Component node. Uses `ThreadPoolExecutor` for parallel drafting. Gracefully skips if Ollama is not running. Zero API cost. |

---

### `graph/`

| File | What it does |
|---|---|
| `graph/__init__.py` | Makes `graph` a Python package. |
| `graph/schema.py` | **Neo4j schema setup.** Creates uniqueness constraints and indexes. Includes indexes for `visibility`, `lifecycle_role`, and a `Specification` uniqueness constraint on `spec_id`. |
| `graph/loader.py` | **Neo4j bulk loader.** Writes UIR objects to Neo4j using MERGE (idempotent). Handles `json.dumps()` for annotation serialization. Writes all v2 fields (visibility, modifiers, lifecycle). Sprint 2 additions: `load_resolves_to_edges()`, `load_specification_nodes()`, `load_implements_spec_edges()`. |
| `graph/gds_client.py` | **Neo4j GDS client.** Runs Leiden community detection dynamically (only projects relationship types that actually exist, preventing crashes on partial ingests). |
| `graph/cleanup.py` | **Incremental cleanup.** Removes stale nodes when a file changes. Does NOT wipe the whole database. |
| `graph/tagger.py` | **EntryPoint/DataSink Tagger.** Labels API endpoints as `:EntryPoint` and DAO/Repository classes as `:DataSink` for flow extraction. |
| `graph/flow_extractor.py` | **GDS Dijkstra Flow Extractor (Sprint 3).** Finds shortest execution paths from each `:EntryPoint` to each `:DataSink` using weighted GDS Dijkstra. CALLS=1, REMOTE_CALLS=5 (cross-service penalty). Returns `FlowPath` objects enriched with config keys and table names. |

---

### `linker/`

| File | What it does |
|---|---|
| `linker/__init__.py` | Makes `linker` a Python package. |
| `linker/maven_resolver.py` | **Maven dependency resolver.** Reads `pom.xml` files → `DEPENDS_ON` edges. |
| `linker/api_bridge.py` | **REST API bridge detector.** Detects Spring + JAX-RS endpoints. Merges class-level `@Path` with method-level `@Path` for accurate effective paths. Extracts `@Consumes` / `@Produces`. Creates `REMOTE_CALLS` edges. |

---

### `vectorstore/`

| File | What it does |
|---|---|
| `vectorstore/__init__.py` | Makes `vectorstore` a Python package. |
| `vectorstore/chunker.py` | **AST-Aware Sliding Window Chunker (Sprint 1).** Produces one or more `EmbeddingChunk` objects per method. Long method bodies (>512 tokens) are split into overlapping 512-token windows with 128-token overlap. Every chunk is prefixed with `[Package: …] [Class: …]` for contextual grounding. Javadoc chunks (code_intent) are never windowed. |
| `vectorstore/embedder.py` | **ChromaDB Embedder.** Default: HuggingFace `SentenceTransformer` (`all-MiniLM-L6-v2`, 384-dim). Optional Sprint 1 upgrade: `FastEmbedder` class using `fastembed` + ONNX Runtime with automatic CUDA detection for GPU-accelerated `nomic-ai/nomic-embed-text-v1.5` (768-dim). |

---

### `community/`

| File | What it does |
|---|---|
| `community/__init__.py` | Makes `community` a Python package. |
| `community/models.py` | Defines the `CommunitySummary` Pydantic model. |
| `community/prompt_builder.py` | **Prompt builder.** Assembles the prompt for community summarisation. Enforces the 8K token budget via `DATA_BUDGET = 6,800` tokens. The while-loop checks `count_tokens(body) <= DATA_BUDGET` directly (bug-fixed in v2). |
| `community/summarizer.py` | **Community summariser.** For each Leiden community: fetches members, builds prompt, calls **fast model** (gpt-4o-mini), stores result in ChromaDB. Parallelised via `ThreadPoolExecutor`. |
| `community/global_rollup.py` | **Global GraphRAG Rollup.** L1 community summaries → L2 sub-system summaries (fast model) → L3 Global Architecture Document (strong model). |

---

### `pipeline/`

| File | What it does |
|---|---|
| `pipeline/__init__.py` | Makes `pipeline` a Python package. |
| `pipeline/mirror.py` | **Git mirror manager.** Reads `sample_repos/repos.yaml`, runs git clone/pull. |
| `pipeline/ingest.py` | **Parallel Ingestion Engine (Sprint 1).** `run_parallel_parse()` uses `ProcessPoolExecutor` to distribute Java file parsing across all CPU cores. Each worker batch runs Tree-sitter + UIRChunker independently in a subprocess (no shared state). Results returned as plain dicts (pickle-safe) and reassembled in the main process. |
| `pipeline/orchestrator.py` | **The main pipeline conductor.** Runs all stages in order: Mirror → Parallel Parse → Link → OSGi Resolution → Load → RFC Grounding → Ollama Micro-Drafts → Post (Leiden + summarisation + tagger + weighted Dijkstra flows + rollup). |
| `pipeline/cli.py` | **Click CLI.** Defines `nexus ingest`, `nexus update`, `nexus validate`, `nexus stats`, `nexus analyze-pr`. |
| `pipeline/incremental.py` | **Incremental updater.** Re-parses only changed files after a PR, updates only affected nodes/vectors. |

---

### `reasoning/`

| File | What it does |
|---|---|
| `reasoning/__init__.py` | Makes `reasoning` a Python package. |
| `reasoning/router.py` | **The query router.** Four routes: A (symbolic/grep), B (entity/Neo4j), C (semantic/ChromaDB), D (global/summaries). Routes A, B, D make zero LLM calls. |
| `reasoning/lexical_search.py` | **Lexical searcher.** Runs ripgrep / git-grep / Python over `./mirror/`. |
| `reasoning/graph_retriever.py` | **Neo4j graph retriever.** Blast-radius expansion, entity lookup, community summary fetching. |
| `reasoning/code_fetcher.py` | **Source code reader.** Reads actual `.java` files from `./mirror/` given file path + line numbers. |
| `reasoning/map_step.py` | **Map step.** Question mode: zero LLM calls (ChromaDB distance → score). PR mode: fast-model LLM scoring with `ThreadPoolExecutor`. |
| `reasoning/reduce_step.py` | **Reduce step.** Strong model (gpt-4o). Intent detection (6 intents). Intent-adaptive output tokens (1,800 or 6,000). Single LLM call → final answer. |
| `reasoning/pipeline.py` | Thin wrapper chaining router → evidence → map → reduce. |
| `reasoning/flow_summarizer.py` | **Flow Narrative Summariser.** Converts GDS Dijkstra `FlowPath` objects into natural-language stories via fast model. |

---

### `sample_repos/`

| File | What it does |
|---|---|
| `sample_repos/repos.yaml` | Repository manifest — lists every Git repo URL and branch to index. Edit this to add/remove repos. |

---

### `rfcs/` (Sprint 4)

| File | What it does |
|---|---|
| `rfcs/*.md` or `rfcs/*.txt` | Place RFC markdown/text files here to enable specification grounding. `parsers/rfc_parser.py` will parse them and link them to Java code that cites them. |

---

### `tests/`

| File | What it tests |
|---|---|
| `tests/test_00_infrastructure.py` | Neo4j, ChromaDB, Redis connectivity |
| `tests/test_01_parser.py` | Java parser: classes, methods, annotations (dict format), modifiers, lifecycle |
| `tests/test_02_mirror.py` | Git mirror cloning/pulling |
| `tests/test_03_linker.py` | Maven resolver, API bridge, JAX-RS path composition |
| `tests/test_04_knowledge_base.py` | Integration: mini ingest → verify Neo4j + ChromaDB |
| `tests/test_05_map_reduce.py` | Map and Reduce steps with sample queries |
| `tests/test_06_incremental.py` | Incremental updater |
| `tests/test_07_community_detection.py` | Leiden + summarisation |
| `tests/test_08_community_prompt.py` | Token budget enforcement (DATA_BUDGET = 6,800 tokens) |

---

## 5. How to Run It

### First time setup (Windows)

```powershell
# 1. Start the databases
docker compose up -d

# 2. Wait ~60 seconds for Neo4j to fully start

# 3. Bootstrap pip into the project's embedded Python environment
py -m ensurepip --upgrade
py -m pip install --upgrade pip

# 4. Install dependencies
py -m pip install -r requirements.txt

# 5. Copy the config template and fill in your OpenAI key
copy .env.example .env
# Edit .env and set LLM_API_KEY=sk-...

# 6. Run the full ingestion pipeline
py main.py ingest
```

### First time setup (Linux/macOS)

```bash
# 1. Start the databases
docker compose up -d

# 2. Create and activate venv
python3 -m venv .venv
source .venv/bin/activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Configure
cp .env.example .env
# Edit .env and set LLM_API_KEY=sk-...

# 5. Run the full ingestion pipeline
python3 main.py ingest
```

### Optional: GPU-accelerated embeddings (Sprint 1)

```bash
# CPU-only (slightly richer than MiniLM):
pip install fastembed onnxruntime

# GPU (requires CUDA 11.x or 12.x):
pip install fastembed-gpu onnxruntime-gpu

# Then enable in .env:
USE_FASTEMBED=true
FASTEMBED_MODEL=nomic-ai/nomic-embed-text-v1.5
```

### Optional: Local LLM micro-drafts via Ollama (Sprint 4)

```bash
# 1. Install Ollama from https://ollama.com/
# 2. Pull and start the model:
ollama run llama3.2

# 3. Enable in .env:
LOCAL_DRAFTING_ENABLED=true
OLLAMA_BASE_URL=http://localhost:11434
OLLAMA_MODEL=llama3.2:3b
```

### What the ingestion pipeline does

1. Clones all repos in `sample_repos/repos.yaml` into `./mirror/`
2. **Parallel-parses** every `.java` file across all CPU cores (`ProcessPoolExecutor`)
3. Applies AST-aware sliding window chunking (512-token windows, 128-token overlap, context prefix)
4. Scans `deployment.toml`, XML, properties, and YAML config files
5. Loads all nodes and edges into Neo4j (with visibility, lifecycle, and annotation properties)
6. Runs OSGi resolution — builds `[:RESOLVES_TO]` edges from `@Component`/`@Reference` annotations
7. Embeds all method chunks into ChromaDB (standard or GPU-accelerated)
8. Parses RFC files and creates `(:Specification)` nodes + `[:IMPLEMENTS_SPEC]` edges
9. Generates Ollama micro-drafts for `:EntryPoint` components (if enabled)
10. Runs Leiden community detection
11. Generates GPT-4o-mini (fast model) summaries for each community
12. Tags API endpoints as `:EntryPoint` and database classes as `:DataSink`
13. Extracts API→Database execution paths via weighted GDS Dijkstra (CALLS=1, REMOTE_CALLS=5)
14. Generates flow narratives for each execution path
15. Generates Global GraphRAG rollup (L2 sub-system + L3 Global Architecture)

### Asking questions

```powershell
py main.py query "does this support multiple audience values?"
py main.py query "what calls TokenExchangeGrantHandler?"
py main.py query "how does the OAuth token refresh flow work?"
py main.py query "what is the overarching architecture of this system?"
```

The last query uses **Route D (Global)** — returns the pre-computed L3 Global
Architecture Document without any LLM call.

### Wiping everything and starting fresh

```powershell
docker compose down -v    # Delete all stored data
docker compose up -d      # Start fresh containers
py main.py ingest         # Re-ingest
```

---

## 6. Common Questions

**Q: Do I need a GPU?**
No. The default HuggingFace embedding model (`all-MiniLM-L6-v2`) runs on CPU.
It's a small 80MB model — no GPU required. If you have a CUDA GPU, you can optionally
enable `USE_FASTEMBED=true` to use the 768-dimensional Nomic model for richer embeddings.

**Q: What Python version do I need?**
Python 3.11 or higher is required (for `tomllib` in the standard library). Python 3.14 is tested and supported.

**Q: On Windows, why use `py` instead of `python`?**
`py` is the Windows Python Launcher — it finds the correct Python installation on your system.
`python` may not be in PATH on some Windows setups. `py` always works.

**Q: How much does OpenAI cost to run?**
Two things call OpenAI: community summarisation (once per ingest, fast model) and the final
reduce step (once per query, strong model). A typical ingest of 2 repos costs ~$0.05–$0.20.
Each query costs < $0.01. A full ingest of 100 repos costs ~$0.50 with the two-tier strategy.
Ollama micro-drafts cost $0 — they run entirely locally.

**Q: Why does the ingestion take a long time?**
The main bottleneck is HuggingFace embedding (hundreds of thousands of method chunks).
Sprint 1 dramatically reduces parse time via `ProcessPoolExecutor` parallel parsing.
Enable GPU embeddings (`USE_FASTEMBED=true`) for another 5–10× speedup on the embed step.

**Q: The ingestion failed halfway. Do I need to restart?**
Not necessarily. Neo4j uses `MERGE` (idempotent) and ChromaDB uses `upsert`. You can re-run
`py main.py ingest` and it will fill in any missing data.

**Q: What is a "community" in this system?**
A community is a cluster of related Java classes and methods automatically detected by the
Leiden algorithm. For example, all classes involved in OAuth token exchange might form one
community; all classes involved in user authentication might form another.

**Q: What does "visibility" mean in the graph?**
Every method and class now has a `visibility` property: `"public"`, `"protected"`, `"private"`,
or `"package"`. This enables security-focused queries like:
`MATCH (n:LogicUnit {visibility: "public"}) WHERE n.annotations CONTAINS '"Value"' RETURN n.fqn`
to find all public methods with Spring `@Value` config injection.

**Q: What is the OSGi lifecycle_role property?**
WSO2 Identity Server uses OSGi (Open Service Gateway initiative) for component lifecycle.
Methods annotated with `@Activate`, `@Deactivate`, or `@Modified` get a `lifecycle_role`
property (`"activate"`, `"deactivate"`, `"modified"`) so you can query:
`MATCH (n:LogicUnit {lifecycle_role: "activate"}) RETURN n.fqn`
to find all OSGi component activation methods.

**Q: What is the `micro_draft` property on Component nodes?**
It's a 3-sentence plain-English summary of what an API endpoint class does, generated by
a local Ollama LLM (e.g. llama3.2:3b). Stored on `:EntryPoint` Component nodes in Neo4j.
Enables zero-cost, offline architectural summaries of every API entry point.

**Q: What are `[:RESOLVES_TO]` edges?**
OSGi service bindings that connect a `@Reference Interface field` in one class to the
`@Component(service={Interface.class})` implementation class that OSGi will inject at runtime.
These edges make OSGi wiring visible in the graph — without them, the call graph would have
gaps wherever OSGi dependency injection is used.

**Q: What are `[:IMPLEMENTS_SPEC]` edges?**
Links from a Java class to an RFC specification node (e.g. `(:Specification {rfc_number: "6749"})`).
Created when `parsers/rfc_parser.py` finds inline RFC citations (`// RFC 6749`) in Java source code.
Enables spec-grounded queries like "which classes implement OAuth 2.0 token exchange?".

**Q: Why are annotations stored as JSON strings in Neo4j?**
Neo4j cannot natively store a list of maps (objects) as a node property. So `list[dict]`
annotation data like `[{"name": "Value", "value": "${key}"}]` is serialized as a JSON string
before writing to Neo4j. You can query it with APOC: `apoc.convert.fromJsonList(n.annotations)`.

**Q: What is the DATA_BUDGET in prompt_builder.py?**
It's `max_context_tokens - SYSTEM_TOKENS - OUTPUT_RESERVE = 8000 - 200 - 1000 = 6800 tokens`.
The community summarisation prompt body is capped at 6,800 tokens so the full LLM call
(system prompt + body + output) fits in the 8,000 token window.

**Q: What does "Batches: 100%|████| 1/1" mean in the terminal output?**
This comes from HuggingFace `SentenceTransformer` converting Java method text into vectors.
`1/1` = one batch processed (small repo). `57/57` = 57 batches (large repo).
This is **only** the embedding step — nothing to do with Neo4j.

**Q: Is the graph database "converted into" a RAG system?**
No — they are two completely separate systems built in parallel and searched independently:
- **Neo4j** (graph) stores *structure*: which method calls which, which class extends which.
- **ChromaDB** (vector) stores *meaning*: each method as 384 or 768 numbers.

Both are retrieval sources. The retrieved context from both is assembled and handed to gpt-4o
which writes the final answer.

**Q: What does the sliding window chunker do differently from Sprint 0?**
Sprint 0's chunker produced exactly one `code_logic` chunk per method, regardless of length.
If a method was 3,000 tokens long, the embedding model would silently truncate it.
Sprint 1's chunker splits long methods into overlapping 512-token windows so the entire
method body is captured. Each window carries the `[Package: …] [Class: …]` prefix so
ChromaDB can locate the source even without the surrounding context.

---

## 7. Reading the Ingest Log

```
graph.schema — Schema setup complete — 12 constraints, 14 indexes
```
→ Neo4j is ready. Created uniqueness rules and speed indexes (includes v2 visibility/lifecycle/spec indexes).

```
httpx — HTTP Request: HEAD https://huggingface.co/sentence-transformers/...
```
→ HuggingFace model checking for a newer version. Downloads once; uses cached copy after that.

```
pipeline.mirror — Cloning https://github.com/... → mirror/identity-server
```
→ Stage 1: `git clone` running for the first time. Subsequent runs show "Pulling".

```
pipeline.ingest — Parallel parse: 3847 files, 8 workers, batch_size=200
```
→ Sprint 1: ProcessPoolExecutor starting. 8 CPU cores will parse files in parallel.

```
pipeline.ingest — Parsed 15/20 batches — 2341 components so far
```
→ Parallel parse progress. Batches completing across worker processes.

```
parsers.java_parser — Parsed 412 classes, 3847 methods from identity-server
```
→ Stage 2: Tree-sitter finished reading all `.java` files. Modifiers, annotations, lifecycle roles extracted.

```
parsers.config_parser — Found 38 configuration entries in identity-server
```
→ Config parser scanned deployment.toml (via tomllib), XML, properties files.

```
parsers.osgi_parser — Built 147 RESOLVES_TO edges from OSGi component bindings
```
→ Sprint 2: OSGi @Component/@Reference annotations resolved to implementation edges.

```
graph.loader — Loaded 3847 LogicUnit nodes
graph.loader — Loaded 12041 CALLS edges
```
→ Stage 3 (Neo4j): Nodes with visibility/modifiers/lifecycle/annotations written to Neo4j.

```
vectorstore.chunker — Generated 5231 chunks from 3847 methods (avg 1.36 chunks/method)
```
→ Sprint 1: Sliding window chunker. Methods >512 tokens produced multiple chunks.

```
Batches: 100%|████████████| 57/57 [02:14<00:00,  2.34s/it]
vectorstore.embedder — Upserted 5231 code_logic chunks
```
→ Stage 3 (ChromaDB): HuggingFace model (or FastEmbed/Nomic if GPU enabled) converted each chunk to a vector.

```
parsers.rfc_parser — Parsed 3 RFC files → 3 Specification nodes
parsers.rfc_parser — Detected 24 IMPLEMENTS_SPEC citations in Java source
```
→ Sprint 4: RFC specification grounding complete.

```
llm.local_drafting — Drafted micro_draft for 18 EntryPoint components via Ollama
```
→ Sprint 4: Ollama llama3.2:3b wrote 3-sentence summaries for each API entry point.

```
gds_client — Running Leiden community detection
community.summarizer — Summarising 14 communities via gpt-4o-mini
```
→ Stage 5: Leiden grouped code into 14 clusters. Fast model (gpt-4o-mini) wrote a plain-English
summary for each cluster. This is the only step that calls OpenAI during ingestion
(unless Ollama micro-drafts are disabled and no RFC files are present).

```
graph.flow_extractor — Flow graph projected: nodes=4821, rels=18947
graph.flow_extractor — Extracted 23 valid execution flows (of 45 pairs evaluated)
```
→ Sprint 3: Weighted GDS Dijkstra found 23 API→Database execution paths.

```
INFO pipeline.orchestrator — Ingestion complete
```
→ Everything finished. Knowledge base ready to query.
