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
| **Vector database** | ChromaDB | *Meaning* — each method converted to 384 numbers so you can search by concept | "What code is related to token validation?" |

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
            │  Tree-sitter reads every .java file and extracts:
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
  (each with visibility, modifier flags, annotations as JSON, community ID)
- **Edges:** 16+ relationship types — CALLS, IMPLEMENTS, EXTENDS, DEPENDS_ON,
  INJECTS, ANNOTATED_WITH, THROWS, OVERRIDES, INSTANTIATES, RETURNS,
  RECEIVES, HANDLES_EVENT, REMOTE_CALLS, CONTAINS, QUERIES_TABLE, READS_CONFIG

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
- `code_logic` — one entry per Java method: the method's source code as a vector
- `code_intent` — one entry per Java method: the method's Javadoc as a vector
- `community_summaries` — plain-English summaries of code clusters
- `flow_narratives` — end-to-end stories describing API-to-database execution paths
- `l2_subsystem_summaries` — sub-system summaries grouping related communities by domain
- `l3_global_architecture` — a single master Global Architecture Document

**How vectors are created:**
For `code_logic` and `code_intent`: a HuggingFace model (`all-MiniLM-L6-v2`)
running locally on your machine converts text into a list of 384 numbers.
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

### OpenAI GPT Models — Two-Tier Strategy

CodeNexus uses two OpenAI models with different cost/quality trade-offs:

| Model | Used for | Why |
|---|---|---|
| **gpt-4o-mini** (fast) | Community summarisation (bulk), map-step scoring, L2 sub-system rollup | ~90% cheaper than gpt-4o; adequate for bulk tasks |
| **gpt-4o** (strong) | Final reduce answer, L3 global rollup only | Best quality for the answer the user sees |

This means a full ingest of 100 repos costs ~$0.50 instead of ~$5+ if everything used gpt-4o.

**What the LLM does NOT do:** It does not search. It does not access the internet.
It only reads the evidence we hand it and synthesises an answer.

**Key constraint:** Every prompt is capped at **8,000 tokens** to control costs.
The `community/prompt_builder.py` `DATA_BUDGET` constant (6,800 tokens) enforces this.

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
string contains, to stay under the 8K context limit.

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
| `config/settings.py` | **Central configuration.** Defines a `Settings` class (Pydantic) that reads every setting from `.env`. All other modules import `settings` from here. Contains `make_llm_client(tier)` factory and `get_model_name(tier)` helper for the two-tier LLM strategy. |

---

### `parsers/`

| File | What it does |
|---|---|
| `parsers/__init__.py` | Makes `parsers` a Python package. |
| `parsers/uir.py` | **Universal Intermediate Representation.** Defines the Python data classes (`Project`, `Module`, `Component`, `LogicUnit`, `Parameter`, `FieldDeclaration`) that hold all parsed Java information. `LogicUnit` now carries `visibility`, `is_static`, `is_abstract`, `is_final`, `is_synchronized`, `lifecycle_role`. All `annotations` fields are `list[dict]` (structured). |
| `parsers/java_parser.py` | **The Java AST parser.** Uses Tree-sitter to extract: class names, method names, Javadoc, method bodies, annotations (as structured dicts), modifiers (visibility/static/abstract), OSGi lifecycle markers, parameter annotations (`@QueryParam`, `@PathVariable`), lambda call bodies, and generic types. |
| `parsers/geid.py` | **Global Entity ID generator.** Creates a 16-character unique identifier: `SHA256(repo_name + "::" + fqn)[:16]`. Shared between Neo4j and ChromaDB. |
| `parsers/fqn_builder.py` | **Fully Qualified Name builder.** Assembles the canonical Java name for a class or method. |
| `parsers/javadoc_parser.py` | Parses Javadoc comment blocks to extract `@param`, `@return`, `@throws`, `@see` tags. |
| `parsers/reflection.py` | Utility for resolving Java type names and handling generic type edge cases. |
| `parsers/sql_schema_parser.py` | **SQL DDL Parser.** Scans `dbscripts/` for `CREATE TABLE` statements → `DatabaseTable` nodes + `QUERIES_TABLE` edges. |
| `parsers/config_parser.py` | **Configuration Parser.** Parses WSO2 `deployment.toml` (using `tomllib` for accurate nested tables), `*.xml`, `*.properties`, and `application.yml`. Creates `Configuration` nodes and `READS_CONFIG` edges. |

---

### `graph/`

| File | What it does |
|---|---|
| `graph/__init__.py` | Makes `graph` a Python package. |
| `graph/schema.py` | **Neo4j schema setup.** Creates uniqueness constraints and indexes. Now includes indexes for `visibility`, `lifecycle_role`, and a `Specification` uniqueness constraint (Sprint 2 prep). |
| `graph/loader.py` | **Neo4j bulk loader.** Writes UIR objects to Neo4j using MERGE (idempotent). Handles `json.dumps()` for annotation serialization. Writes all new v2 fields (visibility, modifiers, lifecycle). |
| `graph/gds_client.py` | **Neo4j GDS client.** Runs Leiden community detection dynamically (only projects relationship types that actually exist, preventing crashes on partial ingests). |
| `graph/cleanup.py` | **Incremental cleanup.** Removes stale nodes when a file changes. Does NOT wipe the whole database. |
| `graph/tagger.py` | **EntryPoint/DataSink Tagger.** Labels API endpoints as `:EntryPoint` and DAO/Repository classes as `:DataSink` for flow extraction. |
| `graph/flow_extractor.py` | **GDS Dijkstra Flow Extractor.** Finds shortest execution paths from each `:EntryPoint` to each `:DataSink` using GDS Dijkstra. Returns `FlowPath` objects enriched with config keys and table names. |

---

### `linker/`

| File | What it does |
|---|---|
| `linker/__init__.py` | Makes `linker` a Python package. |
| `linker/maven_resolver.py` | **Maven dependency resolver.** Reads `pom.xml` files → `DEPENDS_ON` edges. |
| `linker/api_bridge.py` | **REST API bridge detector.** Detects Spring + JAX-RS endpoints. **v2**: merges class-level `@Path` with method-level `@Path` for accurate effective paths. Extracts `@Consumes` / `@Produces`. Creates `REMOTE_CALLS` edges. |

---

### `vectorstore/`

| File | What it does |
|---|---|
| `vectorstore/__init__.py` | Makes `vectorstore` a Python package. |
| `vectorstore/chunker.py` | **Text chunker.** Produces two `EmbeddingChunk` objects per method: one for `code_logic` (method body) and one for `code_intent` (Javadoc). Never cuts across function boundaries. |
| `vectorstore/embedder.py` | **ChromaDB embedder.** Upserts chunks using HuggingFace `SentenceTransformer`. Handles batching and automatic retry on connection errors. |

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
| `pipeline/orchestrator.py` | **The main pipeline conductor.** Runs all stages in order: Mirror → Extract → Link → Load → Post (Leiden + summarisation + tagger + flows + rollup). |
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

### What the ingestion pipeline does

1. Clones all repos in `sample_repos/repos.yaml` into `./mirror/`
2. Parses every `.java` file with Tree-sitter (extracts modifiers, annotations, OSGi lifecycle, etc.)
3. Scans `deployment.toml`, XML, properties, and YAML config files
4. Loads all nodes and edges into Neo4j (with visibility, lifecycle, and annotation properties)
5. Embeds all method bodies and Javadocs into ChromaDB
6. Runs Leiden community detection
7. Generates GPT-4o-mini (fast model) summaries for each community
8. Tags API endpoints as `:EntryPoint` and database classes as `:DataSink`
9. Extracts API→Database execution paths via GDS Dijkstra
10. Generates flow narratives for each execution path
11. Generates Global GraphRAG rollup (L2 sub-system + L3 Global Architecture)

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
No. The HuggingFace embedding model (`all-MiniLM-L6-v2`) runs on CPU.
It's a small 80MB model — no GPU required.

**Q: What Python version do I need?**
Python 3.11 or higher is required (for `tomllib` in the standard library). Python 3.14 is tested and supported.

**Q: On Windows, why use `py` instead of `python`?**
`py` is the Windows Python Launcher — it finds the correct Python installation on your system.
`python` may not be in PATH on some Windows setups. `py` always works.

**Q: How much does OpenAI cost to run?**
Two things call OpenAI: community summarisation (once per ingest, fast model) and the final
reduce step (once per query, strong model). A typical ingest of 2 repos costs ~$0.05–$0.20.
Each query costs < $0.01. A full ingest of 100 repos costs ~$0.50 with the two-tier strategy.

**Q: Why does the ingestion take a long time?**
The bottleneck is Tree-sitter parsing (thousands of Java files) and HuggingFace embedding
(hundreds of thousands of methods). LLM summarisation is parallelised and usually not the bottleneck.

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
- **ChromaDB** (vector) stores *meaning*: each method as 384 numbers.

Both are retrieval sources. The retrieved context from both is assembled and handed to gpt-4o
which writes the final answer.

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
parsers.java_parser — Parsed 412 classes, 3847 methods from identity-server
```
→ Stage 2: Tree-sitter finished reading all `.java` files. Modifiers, annotations, lifecycle roles extracted.

```
parsers.config_parser — Found 38 configuration entries in identity-server
```
→ Config parser scanned deployment.toml (via tomllib), XML, properties files.

```
graph.loader — Loaded 3847 LogicUnit nodes
graph.loader — Loaded 12041 CALLS edges
```
→ Stage 3 (Neo4j): Nodes with visibility/modifiers/lifecycle/annotations written to Neo4j.

```
Batches: 100%|████████████| 57/57 [02:14<00:00,  2.34s/it]
vectorstore.embedder — Upserted 1828 code_logic chunks
```
→ Stage 3 (ChromaDB): HuggingFace model converted each method body to a 384-number vector.

```
gds_client — Running Leiden community detection
community.summarizer — Summarising 14 communities via gpt-4o-mini
```
→ Stage 5: Leiden grouped code into 14 clusters. Fast model (gpt-4o-mini) wrote a plain-English
summary for each cluster. This is the only step that calls OpenAI during ingestion.

```
INFO pipeline.orchestrator — Ingestion complete
```
→ Everything finished. Knowledge base ready to query.
