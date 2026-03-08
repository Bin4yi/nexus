# CodeNexus — Beginner's Guide

> **Who this is for:** Someone who has never worked on this project before and just
> wants to understand what it is, what every piece of technology does, and what
> each file is responsible for.  No prior knowledge of graph databases, vector
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

> **Important:** These are two independent databases. Neo4j is NOT converted into ChromaDB or vice-versa. They are populated in parallel from the same parsed Java code. During a query, both are searched and their results are combined before being handed to the LLM.

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
            │  Tree-sitter reads every .java file
            │  and extracts: class names, method names,
            │  what each method calls, Javadoc comments,
            │  annotations (@Override, @Autowired, etc.)
            ▼
    UIR Objects  (a Python data structure representing
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
  (groups related code into "communities"
   like clustering related topics together)
       │
       ▼
  GPT-4o-mini writes a plain-English summary
  of each community → stored in ChromaDB
       │
       ▼
  NodeTagger labels API endpoints as :EntryPoint
  and database classes as :DataSink (Phase 2)
       │
       ▼
  GDS Dijkstra finds shortest paths from
  each EntryPoint to each DataSink (Phase 2)
       │
       ▼
  GPT-4o-mini writes an "end-to-end story"
  for each execution path → stored in ChromaDB (Phase 2)
       │
       ▼
  Global Rollup groups community summaries into
  sub-system summaries (L2) and a single master
  Global Architecture Document (L3) (Phase 2)
```

### Query (answering your question)

```
Your question: "does this support nested act claims?"
            │
            │  Router classifies the question
            ▼
  ┌─────────────────────────────┐
  │  What kind of question?       │
  └──┬───────┬───────┬──────┬───┘
     │ GLOBAL   │ SYMBOL  │ NAME  │ GENERAL
     ▼         ▼         ▼       ▼
  Return the  Search   Search  Search
  Global      files    Neo4j   ChromaDB
  Architecture directly by exact by meaning
  Document     (grep)  name
     │         │        │       │
     ▼         └───┬────┘       │
  (done!)         │             │
                  ▼             │
           Expand through       │
           the call graph       │
                  │             │
                  └─────┬─────┘
                        ▼
                Assemble code snippets
                + community summaries
                        │
                        ▼
                GPT-4o-mini reads all evidence
                and writes the answer
```

---

## 3. Technology Glossary

### Python
The programming language the entire CodeNexus application is written in.
Version 3.14+ is required.

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
- **Edges:** 14 relationship types — CALLS, IMPLEMENTS, EXTENDS, DEPENDS_ON,
  INJECTS, ANNOTATED_WITH, THROWS, OVERRIDES, INSTANTIATES, RETURNS,
  RECEIVES, HANDLES_EVENT, REMOTE_CALLS, CONTAINS

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
- `community_summaries` — plain-English summaries of code clusters (communities)
- `flow_narratives` — end-to-end architectural stories describing execution paths from API endpoints to database tables (Phase 2)
- `l2_subsystem_summaries` — sub-system summaries grouping related communities by domain (e.g., Authentication, OAuth2, SCIM) (Phase 2)
- `l3_global_architecture` — a single master Global Architecture Document summarising the entire codebase (Phase 2)

**How vectors are created:**
For `code_logic` and `code_intent`: a HuggingFace model (`all-MiniLM-L6-v2`)
running locally on your machine converts text into a list of 384 numbers.
Similar texts produce similar numbers, so searching = "find the entries whose
numbers are closest to my query's numbers". **No OpenAI API call needed here.**

For `community_summaries`, `flow_narratives`, `l2_subsystem_summaries`, and
`l3_global_architecture`: GPT-4o-mini writes the text, then ChromaDB
converts it to a vector automatically.

**Where it runs:** In a Docker container on your machine (port 8000).

---

### Redis
**What it is:** An in-memory key-value store — like a very fast Python dictionary
that lives outside your Python process so it survives restarts.

**Why we use it:** Used to track pipeline state during ingestion (which repos
have been processed, which are pending). If the ingestion crashes halfway, Redis
allows it to resume without starting over.

**Where it runs:** In a Docker container on your machine (port 6379).

---

### Docker / Docker Compose
**What it is:** A tool that runs applications in isolated "containers" — like
lightweight virtual machines. Each container has exactly the software it needs
and nothing else.

**Why we use it:** Instead of asking you to install Neo4j, ChromaDB, and Redis
on your machine (complex, version-sensitive), Docker runs them as containers.
One command (`docker compose up -d`) starts all three.

**`docker-compose.yml`** is the configuration file that defines all three containers.

---

### Tree-sitter
**What it is:** A fast, accurate source code parsing library. It reads source
code and builds an **Abstract Syntax Tree (AST)** — a structured representation
of the code's grammar.

**Why we use it:** When you read a Java file as plain text, you see characters.
Tree-sitter reads it and understands structure: "this is a class declaration,
this is a method, this method has these parameters, it calls these other methods".
This lets us extract all the information without writing a full Java compiler.

**The library used:** `tree-sitter-java` — the Tree-sitter grammar for Java.

---

### HuggingFace SentenceTransformers
**What it is:** A Python library that runs pre-trained AI models locally to
convert text into vectors (numbers representing meaning).

**The model we use:** `all-MiniLM-L6-v2` — a small, fast model that produces
384-dimension vectors. It runs entirely on your CPU, no GPU required, no API
key needed.

**What it does in this project:** Converts every Java method body and every
Javadoc comment into a 384-number vector so ChromaDB can do meaning-based search.

**What "Batches: 100%" means in the terminal:**
When you see this during ingestion:
```
Batches: 100%|████████████████| 1/1 [00:00<00:00, 3.48it/s]
```
This is the HuggingFace model processing a group of method texts and converting
them all into vectors at once. "1/1" means one batch of N texts was processed.
For large repos you might see "57/57" meaning 57 batches were needed.
This is **not** related to Neo4j — it is purely the ChromaDB embedding step.

---

### OpenAI GPT-4o-mini
**What it is:** A large language model (LLM) — an AI that can read text and
write text. GPT-4o-mini is OpenAI's smaller, faster, cheaper variant.

**When we use it (four places during ingestion, one during query):**
1. **Ingestion:** Write plain-English summaries of each Leiden community
2. **Ingestion:** Write end-to-end flow narratives for each API-to-database execution path (Phase 2)
3. **Ingestion:** Write sub-system summaries grouping communities by domain (Phase 2)
4. **Ingestion:** Write the Global Architecture Document covering the entire codebase (Phase 2)
5. **Query:** Read the assembled code evidence and write the answer to your question

**What it does NOT do:** It does not search. It does not access the internet.
It only reads the evidence we hand it and synthesises an answer.

**Key constraint:** Every prompt is capped at **8,000 tokens** (roughly 6,000 words)
to control costs and prevent context overflow.

---

### Leiden Algorithm (via Neo4j GDS)
**What it is:** A community detection algorithm — it looks at the graph of
connections between code entities and groups them into clusters of closely related
code ("communities").

**Why we use it:** Instead of asking the LLM to read all 1 million lines of code,
we ask it to read a summary of the relevant cluster. The Leiden algorithm
automatically finds these clusters from the call graph.

**Neo4j GDS (Graph Data Science):** The Neo4j plugin that runs Leiden and other
graph algorithms directly inside the database — no data export needed.

---

### Pydantic / pydantic-settings
**What it is:** A Python library for defining data structures with automatic
type validation. `pydantic-settings` extends it to read settings from `.env` files.

**Why we use it:** All configuration (Neo4j password, batch sizes, LLM model name, etc.)
lives in a `.env` file and is loaded into a single `Settings` object. No magic
strings scattered through the code.

---

### Tenacity
**What it is:** A Python retry library — wraps any function call so that if it
fails (due to a network hiccup, database overload, etc.) it automatically tries
again with a delay.

**Why we use it:** Neo4j connections can drop during a long 100-repo ingestion
run. Without retries, one network blip would crash the whole pipeline.

---

### Tiktoken
**What it is:** OpenAI's token counting library — counts how many "tokens"
(word pieces) a text string contains, to stay under the 8K limit.

---

### ripgrep (`rg`)
**What it is:** An extremely fast text search tool — like `grep` but written in
Rust for ~10× better speed. Searches through gigabytes of source code in seconds.

**Why we use it:** When the router identifies a specific symbol (like `IMPERSONATED_SUBJECT`),
it runs ripgrep across the entire `./mirror/` directory to find every `.java` file
containing that exact string, with file path and line number.

---

### Poetry / pip + requirements.txt
**What it is:** Python dependency management. `pyproject.toml` and
`requirements.txt` list every Python package the project needs.
`pip install -r requirements.txt` installs them all.

---

## 4. File-by-File Reference

### Root directory

| File | What it does |
|---|---|
| `main.py` | **The main entry point.** Run `python main.py ingest` to ingest repos, `python main.py query "..."` to ask a question. Contains the full query pipeline logic (routing → evidence gathering → LLM synthesis). |
| `docker-compose.yml` | Defines the three Docker services: `nexus-neo4j`, `nexus-chromadb`, `nexus-redis`. Run `docker compose up -d` to start all three. |
| `pyproject.toml` | Python project metadata and build configuration. |
| `requirements.txt` | List of all Python packages the project depends on. |
| `.env` | **Your local config.** Contains passwords, API keys, batch sizes. Never commit this file to Git. |
| `.env.example` | A safe template showing all available settings with default values. Copy this to `.env` and fill in your values. |
| `output.txt` / `output_act.txt` | Sample query output files used for testing and debugging. |

---

### `config/`

| File | What it does |
|---|---|
| `config/__init__.py` | Makes `config` a Python package (empty file). |
| `config/settings.py` | **Central configuration.** Defines a `Settings` class (Pydantic) that reads every setting from `.env`. All other modules import `settings` from here — no magic strings anywhere else in the code. Contains settings for Neo4j, ChromaDB, Redis, LLM, Leiden, batch sizes, retry logic, embedding model, etc. |

---

### `parsers/`
The parsing stage — reads Java source files and converts them into Python data structures.

| File | What it does |
|---|---|
| `parsers/__init__.py` | Makes `parsers` a Python package. |
| `parsers/uir.py` | **Universal Intermediate Representation.** Defines the Python data classes (`Project`, `Module`, `Component`, `LogicUnit`, `Parameter`, `FieldDeclaration`) that hold all parsed Java information. Every other module passes these objects around as the common language. |
| `parsers/java_parser.py` | **The Java AST parser.** Uses Tree-sitter to walk every `.java` file and extract: class names, method names, Javadoc comments, method bodies, parameter types, `@Annotation` names, inheritance (`extends`/`implements`), method calls, `new X()` instantiations, exception `throws` clauses, and `@Override` markers. Produces UIR objects. |
| `parsers/geid.py` | **Global Entity ID generator.** Creates a 16-character unique identifier for every code entity: `SHA256(repo_name + "::" + fully_qualified_name)[:16]`. This same ID is stored in Neo4j and ChromaDB, enabling cross-database lookups. |
| `parsers/fqn_builder.py` | **Fully Qualified Name builder.** Assembles the canonical Java name for a class or method, e.g. `com.example.auth.UserService.getUser(String)`. Used as the primary lookup key across all databases. |
| `parsers/javadoc_parser.py` | Parses Javadoc comment blocks to extract `@param`, `@return`, `@throws`, `@see` tags into structured objects. Used to produce clean `code_intent` embedding text. |
| `parsers/reflection.py` | Utility for resolving Java type names and handling generic type erasure edge cases in the parser. |
| `parsers/sql_schema_parser.py` | **SQL DDL Parser (Phase 2).** Scans `dbscripts/` folders for `CREATE TABLE` statements. Creates `DatabaseTable` nodes in Neo4j and detects DAO classes via regex to create `QUERIES_TABLE` edges linking code to database tables. |
| `parsers/config_parser.py` | **Configuration Parser (Phase 2).** Parses WSO2 `deployment.toml`, `repository/conf/*.xml`, and `application.yml` files. Creates `Configuration` nodes in Neo4j and `READS_CONFIG` edges to Java config-manager classes. |

---

### `graph/`
Everything related to Neo4j — schema, loading data, and running graph algorithms.

| File | What it does |
|---|---|
| `graph/__init__.py` | Makes `graph` a Python package. |
| `graph/schema.py` | **Neo4j schema setup.** Creates all uniqueness constraints (so duplicate nodes are rejected) and indexes (so lookups are fast) the first time the pipeline runs. Defines constraints for `Project`, `Module`, `Component`, `LogicUnit`, `AnnotationType`, `ExceptionType`, `EventClass`, `DatabaseTable`, `Configuration`. |
| `graph/loader.py` | **Neo4j bulk loader.** Takes UIR objects and writes them to Neo4j using Cypher `MERGE` statements (idempotent — safe to run twice). Loads all 4 node types and all 16 relationship types in batches. Also loads `DatabaseTable`, `Configuration` nodes and `QUERIES_TABLE`, `READS_CONFIG` edges. All writes call `.consume()` to prevent Neo4j's lazy-execution silent drops. Protected by `tenacity` retry. |
| `graph/gds_client.py` | **Neo4j GDS client.** Wraps the Graph Data Science plugin API. Runs the Leiden community detection algorithm, fetches community member lists, and lists all community IDs. Dynamically builds the GDS graph projection from whichever relationship types are actually present (resilient to partial ingests). |
| `graph/cleanup.py` | **Incremental cleanup.** Methods for removing stale nodes and edges when a specific file changes (used by the incremental updater). Does NOT wipe the whole database — for a full wipe, use `docker compose down -v`. |
| `graph/tagger.py` | **EntryPoint/DataSink Tagger (Phase 2).** Scans Neo4j for annotation patterns (`@RequestMapping`, `@Path`, `HttpServlet` etc.) and applies `:EntryPoint` secondary labels to API endpoints. DAO/Repository classes receive `:DataSink` labels. Purely Cypher-based — no LLM. |
| `graph/flow_extractor.py` | **GDS Dijkstra Flow Extractor (Phase 2).** Projects execution-flow edges into a GDS graph and runs `gds.shortestPath.dijkstra.stream` for each (EntryPoint, DataSink) pair. Returns `FlowPath` objects enriched with configuration keys and table names. O(E log V) per path. |

---

### `linker/`
Detects and creates cross-class and cross-service relationships that the basic parser can't see.

| File | What it does |
|---|---|
| `linker/__init__.py` | Makes `linker` a Python package. |
| `linker/maven_resolver.py` | **Maven dependency resolver.** Reads `pom.xml` files to find `<dependency>` declarations and creates `DEPENDS_ON` edges between Maven modules in Neo4j. |
| `linker/api_bridge.py` | **REST API bridge detector.** Two-pass detector: Pass 1 registers all Spring `@RequestMapping`, `@GetMapping`, `@PostMapping` endpoints. Pass 2 finds all `RestTemplate`/`WebClient`/`FeignClient` outbound calls. Matches them to create `REMOTE_CALLS` edges between methods in different services. |

---

### `vectorstore/`
Everything related to ChromaDB — chunking method text and embedding it as vectors.

| File | What it does |
|---|---|
| `vectorstore/__init__.py` | Makes `vectorstore` a Python package. |
| `vectorstore/chunker.py` | **Text chunker.** Takes a `LogicUnit` (parsed Java method) and produces two `EmbeddingChunk` objects: one for `code_logic` (the raw method body) and one for `code_intent` (the formatted Javadoc). Chunks by method boundary — never cuts across function borders. |
| `vectorstore/embedder.py` | **ChromaDB embedder.** Takes `EmbeddingChunk` objects and upserts them into ChromaDB using HuggingFace `SentenceTransformer` embeddings. Handles batching (configurable via `EMBEDDING_BATCH_SIZE`). Includes automatic batch-splitting retry for `ConnectionAborted` errors from large payloads. |

---

### `community/`
LLM-based community summarisation — the only step that calls OpenAI during ingestion.

| File | What it does |
|---|---|
| `community/__init__.py` | Makes `community` a Python package. |
| `community/models.py` | Defines the `CommunitySummary` Pydantic model that holds a community's ID, node count, top FQNs, the generated summary text, and metadata. |
| `community/prompt_builder.py` | **Prompt builder for community summarisation.** Assembles the GPT-4o-mini prompt from a community's member list. Enforces the 8K token budget by truncating the member list if needed. |
| `community/summarizer.py` | **Community summariser.** For each Leiden community detected in Neo4j: fetches member FQNs and Javadocs, builds the prompt, calls GPT-4o-mini, and upserts the summary into ChromaDB's `community_summaries` collection. Runs all LLM calls in parallel via `ThreadPoolExecutor`. |
| `community/global_rollup.py` | **Global GraphRAG Rollup (Phase 2).** Microsoft GraphRAG hierarchical rollup. Groups L1 community summaries into 11 domains (Authentication, OAuth2, SCIM, Federation, etc.) to produce L2 sub-system summaries, then rolls all L2 into a single L3 Global Architecture Document. Stored in ChromaDB `l2_subsystem_summaries` + `l3_global_architecture`. |

---

### `pipeline/`
Orchestration — wires all the stages together into one runnable pipeline.

| File | What it does |
|---|---|
| `pipeline/__init__.py` | Makes `pipeline` a Python package. |
| `pipeline/mirror.py` | **Git mirror manager.** Reads `sample_repos/repos.yaml`, then runs `git clone` (first time) or `git pull` (subsequent runs) for each repo into the `./mirror/` directory. |
| `pipeline/orchestrator.py` | **The main pipeline conductor.** The `IngestionPipeline` class that runs all stages in order: Mirror → Extract (parse all Java files) → Link (create all edge types) → Load (write to Neo4j + ChromaDB) → Post (Leiden + summarisation). Called by `python main.py ingest`. |
| `pipeline/cli.py` | **Click CLI for pipeline operations.** Defines `nexus ingest`, `nexus update`, `nexus validate`, `nexus stats`, `nexus analyze-pr` commands. Used when you install the project as a CLI tool rather than running `python main.py` directly. |
| `pipeline/incremental.py` | **Incremental updater.** Used after a single repo's PR is merged. Re-parses only the changed files, updates only the affected Neo4j nodes and ChromaDB vectors, and optionally re-runs community summarisation. Much faster than re-running the full pipeline. |

---

### `reasoning/`
The query engine — takes a question and produces an answer.

| File | What it does |
|---|---|
| `reasoning/__init__.py` | Makes `reasoning` a Python package. |
| `reasoning/router.py` | **The query router.** Classifies every incoming question into one of four routes — SYMBOLIC (search by exact code symbol), EXACT-ENTITY (search by class/method name), SEMANTIC (search by meaning), or GLOBAL (pre-computed architecture summaries). Routes A and B are 100% deterministic; route C uses vector search; route D returns L3/L2 summaries directly. |
| `reasoning/lexical_search.py` | **Lexical (grep) searcher.** Runs ripgrep, git-grep, or pure-Python grep over the `./mirror/` directory to find exact text matches. Returns file path + line number + matching line. The backbone of the deterministic routing. |
| `reasoning/graph_retriever.py` | **Neo4j graph retriever.** Given a set of starting FQNs (seed nodes), traverses the call graph up to N hops to find all affected code (the "blast radius"). Also supports entity lookup by name, grep-to-graph bridging (given a file + line number, find the Neo4j node that covers that line), and community summary fetching by ID. All calls use `tenacity` retry. |
| `reasoning/code_fetcher.py` | **Source code reader.** Given a file path and line numbers, reads the actual `.java` file from `./mirror/` and returns the source code as a string. Used to include real code in the LLM context (rather than just FQN names). |
| `reasoning/map_step.py` | **Map step of Map-Reduce.** For semantic/conceptual queries: fetches all community summaries from ChromaDB and asks GPT-4o-mini to score each one for relevance to the question. Runs all scoring calls in parallel. Returns the top-scoring communities for the Reduce step. Has dual-mode prompts: PR blast-radius mode and user question mode. |
| `reasoning/reduce_step.py` | **Reduce step.** Takes all the assembled evidence (community summaries + code snippets + file targets) and calls GPT-4o-mini once to synthesise a final answer. Uses intent-aware prompts: capability queries get the anti-hallucination SYSTEM_CAPABILITY prompt with rigorous 4-step reasoning rules; code queries get the code-display prompt; PR queries get the blast-radius analysis prompt. Enforces the 8K token hard limit. |
| `reasoning/pipeline.py` | **Reasoning pipeline wrapper.** Thin wrapper that chains router → evidence gathering → map (if needed) → reduce into a single `run(question)` call. Used by external callers who want one function to call. |
| `reasoning/flow_summarizer.py` | **Flow Narrative Summariser (Phase 2).** Converts GDS Dijkstra execution paths (`FlowPath` objects) into natural-language "End-to-End Architectural Stories" via GPT-4o-mini. Each story describes the business process, data transformations, services crossed, tables written to, and config keys consulted. Stored in ChromaDB `flow_narratives` collection. |

---

### `sample_repos/`

| File | What it does |
|---|---|
| `sample_repos/repos.yaml` | **Repository manifest.** Lists every Git repository URL and branch to be cloned and indexed. Edit this to add or remove repos from the knowledge base. |

---

### `mirror/`
Local Git clones of all indexed repositories. Created and managed by `pipeline/mirror.py`.
This directory is large (all source code) and should be in `.gitignore`.

---

### `tests/`

| File | What it does |
|---|---|
| `tests/__init__.py` | Makes `tests` a Python package. |
| `tests/conftest.py` | Shared pytest fixtures — sets up test database connections, creates test repos, etc. |
| `tests/test_00_infrastructure.py` | Tests that Neo4j, ChromaDB, and Redis are reachable and healthy. |
| `tests/test_01_parser.py` | Tests that the Java parser correctly extracts classes, methods, and relationships from sample `.java` files. |
| `tests/test_02_mirror.py` | Tests that the git mirror cloning/pulling works correctly. |
| `tests/test_03_linker.py` | Tests that Maven and API bridge linkers create the correct edges. |
| `tests/test_04_knowledge_base.py` | Integration test — runs a mini ingestion and verifies Neo4j + ChromaDB contain the expected data. |
| `tests/test_05_map_reduce.py` | Tests that the Map and Reduce steps produce reasonable output for sample queries. |
| `tests/test_06_incremental.py` | Tests that the incremental updater correctly updates only changed files. |
| `tests/fixtures/UserService.java` | A simple sample Java file used by the parser tests. |
| `tests/fixtures/pom.xml` | A sample Maven POM used by the linker tests. |

---

### `docs/`

| File | What it does |
|---|---|
| `docs/ARCHITECTURE.md` | Full technical architecture documentation with diagrams. Intended for developers who already understand the tech stack. |
| `docs/BEGINNERS_GUIDE.md` | This file — explains everything from scratch for newcomers. |
| `docs/CODE_REFERENCE.md` | Reference for every public class and method. |
| `docs/IMPLEMENTATION_PLAN.md` | The original roadmap and task breakdown used to build the system. |
| `docs/PHASE01_DIAGRAM_EXPLAINED.md` | Walkthrough of the Phase 1 data flow diagram. |
| `docs/PROJECT_PROPOSAL.md` | The original proposal document describing the business goals. |

---

## 5. How to Run It

### First time setup

```powershell
# 1. Start the databases
docker compose up -d

# 2. Wait ~60 seconds for Neo4j to fully start, then activate Python environment
.venv\Scripts\Activate.ps1

# 3. Install dependencies (if not already done)
pip install -r requirements.txt

# 4. Copy the config template and fill in your OpenAI key
copy .env.example .env
# Edit .env and set OPENAI_API_KEY=sk-...

# 5. Run the full ingestion pipeline
python main.py ingest
```

The ingestion pipeline will:
1. Clone all repos listed in `sample_repos/repos.yaml` into `./mirror/`
2. Parse every `.java` file with Tree-sitter
3. Scan `dbscripts/` folders for SQL DDL → create `DatabaseTable` nodes (Phase 2)
4. Scan configuration files (`deployment.toml`, `*.xml`, `*.yml`) → create `Configuration` nodes (Phase 2)
5. Load all nodes and edges into Neo4j
6. Embed all method bodies and Javadocs into ChromaDB
7. Run Leiden community detection
8. Generate GPT-4o-mini summaries for each community
9. Tag API endpoints as `:EntryPoint` and database classes as `:DataSink` (Phase 2)
10. Extract EntryPoint→DataSink execution paths via GDS Dijkstra (Phase 2)
11. Generate flow narratives for each execution path (Phase 2)
12. Generate Global GraphRAG rollup (L2 sub-system summaries + L3 Global Architecture) (Phase 2)

### Asking questions

```powershell
python main.py query "does this support multiple audience values?"
python main.py query "what calls TokenExchangeGrantHandler?"
python main.py query "how does the OAuth token refresh flow work?"
python main.py query "what is the overarching architecture of this system?"
```

The last query uses **Route D (Global)** — it returns the pre-computed L3 Global
Architecture Document directly, without running vector search or LLM scoring.

### Wiping everything and starting fresh

```powershell
# Delete all stored data (Neo4j + ChromaDB + Redis volumes)
docker compose down -v

# Start fresh containers
docker compose up -d

# Re-ingest
python main.py ingest
```

---

## 6. Common Questions

**Q: Do I need a GPU?**
No. The HuggingFace embedding model (`all-MiniLM-L6-v2`) runs on CPU.
It's a small 80MB model — no GPU required.

**Q: How much disk space does it need?**
Each cloned repository takes its full source size. For the two sample WSO2 repos
this is roughly 500MB–1GB. Neo4j and ChromaDB data volumes add ~500MB more.

**Q: How much does OpenAI cost to run?**
Only two things call OpenAI: community summarisation (once per ingest) and
the final reduce step (once per query). A typical ingest of 2 repos costs
roughly $0.05–$0.20 in API credits. Each query costs < $0.01.

**Q: Why does the ingestion take a long time?**
The bottleneck is the Tree-sitter parser (thousands of Java files) and the
HuggingFace embedding (hundreds of thousands of methods). The LLM summarisation
is parallelised and is usually not the bottleneck.

**Q: The ingestion failed halfway. Do I need to restart from the beginning?**
Not necessarily. The pipeline uses `MERGE` in Neo4j (idempotent — won't create
duplicates) and `upsert` in ChromaDB (also idempotent). You can simply re-run
`python main.py ingest` and it will continue from where data is missing.

**Q: What does "deterministic" mean in this context?**
It means the answer does not depend on random chance or the LLM's creativity.
For questions about specific code symbols (Route A) and named entities (Route B),
the system finds the answer through pure database queries — the LLM only formats
the result. The same question always produces evidence from the same code.
Route D (Global) is also deterministic — it returns pre-computed summaries.

**Q: What is a "community" in this system?**
A community is a cluster of related Java classes and methods automatically
detected by the Leiden algorithm. For example, all the classes involved in
OAuth token exchange might form one community; all the classes involved in
user authentication might form another. Each community gets a plain-English
summary written by GPT-4o-mini.

**Q: What are EntryPoints and DataSinks? (Phase 2)**
EntryPoints are the starting points of execution flows — API endpoints, servlet
handlers, and web framework triggers (detected by annotations like `@RequestMapping`,
`@Path`, and class names ending in `Controller`/`Servlet`/`Endpoint`).
DataSinks are the terminal points — DAO classes, repository classes, and any
code that executes SQL queries (detected by `@Repository`, class name patterns,
and `QUERIES_TABLE` edges). The system finds shortest paths between them using
GDS Dijkstra to map end-to-end execution flows.

**Q: What is the Global Architecture Document? (Phase 2)**
A single master document that summarises the entire codebase's architecture.
Built using the Microsoft GraphRAG hierarchical rollup: L1 community summaries
are grouped into 11 domain-specific L2 sub-system summaries (Authentication,
OAuth2, SCIM, Federation, etc.), then all L2 summaries are rolled into one L3
Global Architecture Document. When you ask "what is the overarching architecture?",
Route D returns this document directly.

**Q: Why does ChromaDB show as "unhealthy" in Docker but still work?**
The Docker healthcheck URL in `docker-compose.yml` was updated to the correct
endpoint (`/api/v1`). After a `docker compose down -v && docker compose up -d`
the healthcheck will show correctly. The server itself works fine regardless.

**Q: What does "Batches: 100%|████| 1/1" mean in the terminal output?**
This progress bar comes from the HuggingFace `SentenceTransformer` model
(`all-MiniLM-L6-v2`) while it is converting Java method text into number vectors.
It processes text in groups called *batches* for efficiency.
- `1/1` = one batch was processed (a small repo with few methods)
- `57/57` = 57 batches were needed (a large repo with many methods)

This is **only** the embedding step (text → numbers for ChromaDB).
It has nothing to do with Neo4j or the graph database.

**Q: Is the graph database "converted into" a RAG system?**
No — they are two completely separate systems that are built in parallel and
searched independently:

- **Neo4j** (graph database) — stores *structure*: which method calls which,
  which class extends which. Queried using Cypher graph traversal.
- **ChromaDB** (vector database) — stores *meaning*: each method as 384 numbers,
  searched by semantic similarity. This is the RAG part.

**RAG** (Retrieval-Augmented Generation) is the technique of *retrieving*
relevant context before asking the LLM to generate an answer. CodeNexus uses
*both* Neo4j and ChromaDB as retrieval sources — the retrieved context from
both is assembled and handed to GPT-4o-mini which then writes the answer.
Neither database is derived from the other.

---

## 7. Reading the Ingest Log

When you run `python main.py ingest` you will see many log lines scroll past.
Here is what each important line means:

```
graph.schema — Schema setup complete — 9 constraints, 10 indexes
```
→ Neo4j is ready. It created the uniqueness rules and speed indexes
  that prevent duplicate nodes and make lookups fast.

```
httpx — HTTP Request: HEAD https://huggingface.co/sentence-transformers/...
```
→ The HuggingFace embedding model is checking if it needs to download
  a newer version of `all-MiniLM-L6-v2`. It only downloads once; after
  that it uses the cached copy on disk.

```
pipeline.mirror — Cloning https://github.com/... → mirror/identity-server
```
→ Stage 1 (Mirror): `git clone` is running for the first time on this repo.
  Subsequent runs will show "Pulling" instead.

```
parsers.java_parser — Parsed 412 classes, 3847 methods from identity-server
```
→ Stage 2 (Extract): Tree-sitter has finished reading all `.java` files in
  this repo and produced UIR objects (Python data structures) representing
  every class and method found.

```
graph.loader — Loaded 3847 LogicUnit nodes
graph.loader — Loaded 12041 CALLS edges
```
→ Stage 3 (Load — Neo4j): The parsed classes, methods, and relationships
  have been written to the Neo4j graph database in batches.

```
Batches: 100%|████████████| 57/57 [02:14<00:00,  2.34s/it]
vectorstore.embedder — Upserted 1828 code_logic chunks
```
→ Stage 3 (Load — ChromaDB): The HuggingFace model converted each method
  body into a 384-number vector. "57/57" means it processed 57 batches of
  methods. Then ChromaDB stored all 1828 vectors. This is the embedding step.

```
Batches: 100%|████████████| 57/57 [00:48<00:00, ...]
vectorstore.embedder — Upserted 1828 code_intent chunks
```
→ Same as above but for the Javadoc text (`code_intent` collection) instead
  of the method body (`code_logic` collection). Each method gets two vectors.

```
parsers.config_parser — Found 38 configuration entries in identity-server
graph.loader — Loaded 38 Configuration nodes
graph.loader — Loaded 251 READS_CONFIG edges
```
→ Stage 4 (Phase 2 extras): The config parser scanned `deployment.toml` and
  XML config files, found 38 config keys, and linked them to the Java classes
  that read those keys. These appear as `Configuration` nodes in Neo4j.

```
gds_client — Running Leiden community detection
community.summarizer — Summarising 14 communities via GPT-4o-mini
```
→ Stage 5 (Post): First the Leiden algorithm ran inside Neo4j GDS and
  grouped code into 14 clusters. Then GPT-4o-mini wrote a plain-English
  summary for each cluster. This is the only step that calls OpenAI.

```
INFO pipeline.orchestrator — Ingestion complete
```
→ Everything finished successfully. The knowledge base is ready to query.

### The complete data flow, annotated

```
 repos.yaml
     │
     │  Stage 1: git clone / git pull
     ▼
 ./mirror/   ← raw .java files on disk
     │
     │  Stage 2: Tree-sitter AST parser
     │  (parsers/java_parser.py)
     ▼
 UIR objects  ← Python objects (classes, methods, calls)
     │
     ├─── Stage 3a: graph/loader.py ──────────────────► Neo4j
     │         writes nodes + edges via Cypher MERGE       (graph database)
     │         "who calls who, what implements what"
     │
     └─── Stage 3b: vectorstore/embedder.py ──────────► ChromaDB
               HuggingFace model converts text → vectors   (vector database)
               "Batches" progress bar appears here
               "what does each method mean"

   ↑ These two stores are built from the SAME UIR objects.
     They are INDEPENDENT — neither is derived from the other.

 Stage 4 (Phase 2 extras, also from UIR objects):
   parsers/sql_schema_parser.py  →  DatabaseTable nodes in Neo4j
   parsers/config_parser.py      →  Configuration nodes in Neo4j

 Stage 5 (post-load):
   graph/gds_client.py  →  Leiden algorithm inside Neo4j
                            groups methods into communities
   community/summarizer.py  →  GPT-4o-mini writes a plain-English
                                summary for each community
                                → stored in ChromaDB

   graph/tagger.py       →  labels EntryPoint / DataSink nodes (Phase 2)
   graph/flow_extractor.py →  GDS Dijkstra finds API→DB paths (Phase 2)
   reasoning/flow_summarizer.py → GPT-4o-mini writes flow stories (Phase 2)
   community/global_rollup.py   → GPT-4o-mini writes L2/L3 architecture (Phase 2)
```
