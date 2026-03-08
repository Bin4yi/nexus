# CodeNexus — Architecture & Workflow Documentation

## Overview

CodeNexus is a **headless GraphRAG knowledge base engine** that compiles Java source code from
100+ Git repositories into a hybrid database — a structural property graph in Neo4j and semantic
vector embeddings in ChromaDB. It is the foundational "World Model" for autonomous multi-agent
CI/CD pipelines.

### Design Principles

| Principle | Implementation |
|---|---|
| **ZERO hallucinations (symbolic/exact)** | Three-tier deterministic router — Routes A & B never invoke semantic search |
| **Centralised config** | Every constant reads from `config/settings.py` → `.env` — no hardcoding |
| **Two-tier LLM cost strategy** | `llm_fast_model` (gpt-4o-mini) for ~90% of calls; `llm_strong_model` (gpt-4o) for final answers only |
| **Resilience** | `tenacity` retry decorators on all Neo4j, GDS, and LLM calls |
| **Scale** | ThreadPoolExecutor for summarisation, APOC batch for Neo4j, settings-driven concurrency |
| **Intent-adaptive token budget** | Reduce step detects 6 query intents; `narrative`/`general` get 6,000 output tokens; safety/capability/impact/code get 1,800 |
| **Azure OpenAI** | `settings.make_llm_client(tier)` factory switches between `openai.OpenAI` and `openai.AzureOpenAI` based on `LLM_PROVIDER`; all modules call the factory |
| **Zero-LLM Map step for questions** | `mode=question` converts ChromaDB cosine distance directly to a 0–100 score |
| **High-fidelity Java parsing (v2)** | Method visibility/modifiers, structured annotation dicts, generic type preservation, OSGi lifecycle, parameter annotations, JAX-RS path composition, lambda call extraction |

---

## System Architecture

```
Git Repositories (100+)
      │
      ▼
┌─────────────────────────────────────────────────────────────┐
│             STAGE 1 — Mirror                                │
│  RepositoryMirror (pipeline/mirror.py)                      │
│  git clone / git pull → ./mirror/                           │
└─────────────────────────┬───────────────────────────────────┘
                          │ .java, .xml, .toml files
                          ▼
┌─────────────────────────────────────────────────────────────┐
│             STAGE 2 — Extract                               │
│  JavaParser (parsers/java_parser.py)                        │
│  • Tree-sitter AST → UIR objects                            │
│  • Method visibility + modifiers (public/static/abstract)   │
│  • Annotations as structured dicts [{"name":"Value",...}]   │
│  • Generic type preservation (List<User> preserved)         │
│  • OSGi lifecycle detection (@Activate → lifecycle_role)    │
│  • Lambda/stream body call extraction                       │
│  • Parameter-level annotations (@QueryParam, @PathVariable) │
│                                                             │
│  ConfigurationParser (parsers/config_parser.py)             │
│  • deployment.toml → tomllib (Python 3.11+) with fallback   │
│  • *.xml → element names + ${placeholder} extraction        │
│  • *.properties → key=value pairs                           │
│  • application.yml → top-level YAML keys                    │
└─────────────────────────┬───────────────────────────────────┘
                          │ UIR objects (memory)
                          ▼
┌─────────────────────────────────────────────────────────────┐
│             STAGE 3 — Link                                  │
│  MavenResolver (linker/maven_resolver.py)                   │
│  • pom.xml → DEPENDS_ON edges                               │
│                                                             │
│  ApiBridgeDetector (linker/api_bridge.py)                   │
│  • Spring @RequestMapping → endpoint registry               │
│  • JAX-RS: class @Path + method @Path merged → effective path│
│  • @Consumes / @Produces extracted                          │
│  • HTTP client calls → REMOTE_CALLS edges                   │
└─────────────────────────┬───────────────────────────────────┘
                          │ enriched UIR + edges
                          ▼
┌─────────────────────────────────────────────────────────────┐
│             STAGE 4 — Load                                  │
│  Neo4j Bulk Loader (graph/loader.py)                        │
│  • MERGE nodes (visibility, is_static, lifecycle_role, ...)  │
│  • annotations stored as json.dumps(list[dict])             │
│  • 16 relationship types via APOC batch                     │
│                                                             │
│  ChromaDB Embedder (vectorstore/embedder.py)                │
│  • all-MiniLM-L6-v2 (384-dim, CPU, no API key)              │
│  • code_logic collection (method bodies)                    │
│  • code_intent collection (Javadoc text)                    │
└─────────────────────────┬───────────────────────────────────┘
                          │
                          ▼
┌─────────────────────────────────────────────────────────────┐
│             STAGE 5 — Post-Processing                       │
│  GDSClient.run_leiden()                                     │
│  • Dynamic GDS graph projection (resilient to partial ingests)│
│  • Leiden community detection → community_id on each node   │
│                                                             │
│  CommunitySummarizer (community/summarizer.py)              │
│  • fast model (gpt-4o-mini) per community                   │
│  • ThreadPoolExecutor (SUMMARIZER_MAX_WORKERS parallel calls)│
│  • → community_summaries ChromaDB collection               │
│                                                             │
│  NodeTagger → :EntryPoint / :DataSink labels               │
│  FlowExtractor → GDS Dijkstra → FlowPath objects           │
│  FlowNarrativeSummarizer → fast model → flow_narratives    │
│  GlobalRollup → L2 (fast model) + L3 (strong model)        │
└─────────────────────────────────────────────────────────────┘
```

---

## Query Architecture

```
Your question
      │
      ▼  QueryRouter.route()
  ┌───────────────────────────────────┐
  │ Route A: SYMBOLIC                 │
  │   exact code symbol in question   │
  │   → ripgrep → Neo4j FQN lookup    │
  │   → zero LLM calls               │
  │                                   │
  │ Route B: EXACT-ENTITY             │
  │   named class/method              │
  │   → Neo4j MATCH by name           │
  │   → zero LLM calls               │
  │                                   │
  │ Route C: SEMANTIC                 │
  │   conceptual / natural language   │
  │   → ChromaDB vector search        │
  │   → MapStep (0 LLM calls)        │
  │   → ReduceStep (1 strong model)  │
  │                                   │
  │ Route D: GLOBAL                   │
  │   architecture overview           │
  │   → return L3/L2 summaries        │
  │   → zero LLM calls               │
  └───────────────────────────────────┘
         │ (Routes A, B, C)
         ▼
  GraphRetriever.get_blast_radius()
  → expand seed FQNs up to N hops
  → collect community IDs
  → fetch community summaries (ChromaDB)
  → fetch code snippets (./mirror/ files)
         │
         ▼
  ReduceStep.run() — strong model (gpt-4o)
  → intent detection (6 intents)
  → token budget allocation (1800 or 6000 output tokens)
  → single LLM call → final answer
```

---

## Neo4j Data Model

### Node Labels and Key Properties

| Label | Key Properties |
|---|---|
| `Project` | `geid`, `name`, `url`, `branch` |
| `Module` | `geid`, `name`, `group_id`, `artifact_id`, `version` |
| `Component` | `geid`, `fqn`, `kind`, `docstring`, `visibility`, `is_abstract`, `is_final`, `annotations` (JSON), `community_id` |
| `LogicUnit` | `geid`, `fqn`, `kind`, `body_text`, `docstring`, `visibility`, `is_static`, `is_abstract`, `is_final`, `is_synchronized`, `lifecycle_role`, `annotations` (JSON), `community_id` |
| `AnnotationType` | `name` |
| `ExceptionType` | `fqn` |
| `EventClass` | `fqn` |
| `DatabaseTable` | `name`, `repo_name`, `columns` |
| `Configuration` | `config_key`, `config_type`, `source_file`, `repo_name` |
| `Specification` | `spec_id`, `rfc`, `section`, `title`, `text`, `rfc_title` (Sprint 2) |

### Relationship Types (16)

| Type | From → To | Meaning |
|---|---|---|
| `CONTAINS` | Project → Module | Repository containment |
| `DECLARES` | Module → Component | Maven artifact contains class |
| `HAS_METHOD` | Component → LogicUnit | Class contains method |
| `HAS_FIELD` | Component → Component | Field reference |
| `CALLS` | LogicUnit → LogicUnit | Direct method call |
| `IMPLEMENTS` | Component → Component | Interface implementation |
| `EXTENDS` | Component → Component | Class inheritance |
| `DEPENDS_ON` | Module → Module | Maven dependency |
| `INJECTS` | LogicUnit → Component | DI injection (`@Autowired`, `@Reference`) |
| `REMOTE_CALLS` | LogicUnit → LogicUnit | Cross-service REST call |
| `THROWS` | LogicUnit → ExceptionType | Exception declaration |
| `OVERRIDES` | LogicUnit → LogicUnit | Method override |
| `INSTANTIATES` | LogicUnit → Component | `new X()` creation |
| `HANDLES_EVENT` | Component → EventClass | WSO2 event handler |
| `ANNOTATED_WITH` | Component/LogicUnit → AnnotationType | Annotation usage |
| `RETURNS` / `RECEIVES` | LogicUnit → Component | Return/param type |
| `QUERIES_TABLE` | Component → DatabaseTable | DAO accesses table |
| `READS_CONFIG` | Component → Configuration | Class reads config key |
| `IMPLEMENTS_SPEC` | LogicUnit → Specification | Code implements RFC (Sprint 2) |
| `RESOLVES_TO` | Component → Component | OSGi service resolution (Sprint 2) |

---

## ChromaDB Collections

| Collection | Document | Metadata | Use Case |
|---|---|---|---|
| `code_logic` | Method body text | `geid`, `fqn`, `file_path`, `start_line` | "Find code that does X" |
| `code_intent` | Javadoc description | `geid`, `fqn`, `file_path` | "Find code intended for X" |
| `community_summaries` | LLM community summary | `community_id`, `node_count`, `llm_model` | Route C map step |
| `flow_narratives` | End-to-end flow story | `flow_id`, `entry_fqn`, `sink_fqn` | Architecture trace questions |
| `l2_subsystem_summaries` | Domain summary | `domain`, `community_count` | Sub-system overviews |
| `l3_global_architecture` | Global arch document | `generated_at`, `community_count` | Route D global queries |

---

## Token Budget Architecture

```
max_context_tokens = 8000 (configurable)
│
├── SYSTEM_TOKENS = 200   (system prompt reservation)
├── DATA_BUDGET = 6800    (prompt body budget)
│   ├── SNIPPET_BUDGET    (50% of DATA_BUDGET — code evidence)
│   └── SUMMARY_BUDGET    (40% of DATA_BUDGET — community summaries)
└── OUTPUT_RESERVE        (1800 for safety/capability/impact/code)
                          (6000 for narrative/general)
```

The `prompt_builder.py` DATA_BUDGET check (`count_tokens(body) <= DATA_BUDGET`) ensures the
community prompt body never exceeds 6,800 tokens — leaving room for the system prompt and
model output in the 8,000 token ceiling.

---

## GEID Bridge

Every entity in both Neo4j and ChromaDB shares the same 16-character GEID:

```python
geid = sha256(f"{repo_name}::{fqn}").hexdigest()[:16]
```

Properties:
- **Deterministic**: same input → same GEID on every ingest
- **Cross-DB**: the primary key in both Neo4j nodes and ChromaDB metadata
- **Repository-scoped**: same class name in two repos → different GEIDs

Usage pattern:
```python
# 1. Semantic search finds a method by meaning
results = chroma.query(query_texts=["token validation"])
geid = results["metadatas"][0]["geid"]

# 2. Immediately jump to the graph to find blast radius
blast = neo4j.run("MATCH (n {geid: $g})<-[:CALLS*1..5]-(m) RETURN m.fqn", g=geid)
```

---

## Annotation Storage Format

Annotations are stored in Neo4j as JSON-serialized `list[dict]`:

```python
# In memory (UIR)
lu.annotations = [
    {"name": "Override"},
    {"name": "Value", "value": "${oauth.token.endpoint}"},
    {"name": "Reference", "cardinality": "MANDATORY"},
]

# In Neo4j (property value)
annotations = '[{"name": "Override"}, {"name": "Value", "value": "${oauth.token.endpoint}"}, ...]'

# Query with APOC
MATCH (n:LogicUnit)
WHERE any(a IN apoc.convert.fromJsonList(n.annotations) WHERE a.name = 'Value')
RETURN n.fqn
```

This structured format enables:
- Finding all methods with `@QueryParam("client_id")`
- Detecting `@Reference(cardinality=MANDATORY)` OSGi wiring
- Extracting `@Value("${key}")` config key links

---

## v2 Sprint 1 Changes Summary

All changes are backwards-compatible. Existing data in Neo4j gains new optional properties.

### `parsers/uir.py`
- `LogicUnit`: added `visibility`, `is_static`, `is_abstract`, `is_final`, `is_synchronized`, `lifecycle_role`
- `Component`: added `visibility`, `is_abstract`, `is_final`
- `Parameter`: added `annotations: list[dict]`
- `LogicUnit.annotations` and `Component.annotations`: changed from `list[str]` to `list[dict]`

### `parsers/java_parser.py`
- New `_parse_annotation(node, source) → dict` — structured annotation extraction
- Updated `_extract_annotations()` to use `_parse_annotation()` for all nodes
- Updated `_extract_parameters()` to extract parameter-level annotations
- Modifier extraction from `modifiers` tree-sitter node
- OSGi lifecycle detection (`@Activate`, `@Deactivate`, `@Modified`)
- Updated `@Override` detection to work with dict annotations
- Lambda body call extraction via `_walk_calls()` recursion
- Generic type preservation in field/return/parameter types

### `linker/api_bridge.py`
- Class-level `@Path` base path detection and composition with method paths
- `@Consumes` / `@Produces` extraction into `EndpointRegistration`

### `parsers/config_parser.py`
- `tomllib`-based TOML parsing (Python 3.11+) with regex fallback
- `_scan_properties_file()` for `*.properties` files
- Nested TOML table flattening via `_flatten_toml_dict()`

### `graph/loader.py`
- `import json` for annotation serialization
- Updated `_load_component` and `_load_logic_unit` with new properties
- Updated `load_annotated_with` to handle both dict and string annotation formats

### `graph/schema.py`
- New indexes: `logicunit_visibility`, `component_visibility`, `logicunit_lifecycle`, `spec_rfc`
- New constraint: `Specification.spec_id` (Sprint 2 prep)

### `config/settings.py`
- Added `llm_fast_model` (default: `gpt-4o-mini`) and `llm_strong_model` (default: `gpt-4o`)
- Updated `make_llm_client(tier="fast")` to accept `tier` parameter
- Added `get_model_name(tier) → str` helper

### `community/prompt_builder.py`
- Fixed token budget bug: while loop now checks `count_tokens(body) <= DATA_BUDGET` directly
  instead of checking `count_tokens(SYSTEM_PROMPT + body) <= max_context - output_reserve`

---

## v2 Sprint 2–4 Roadmap

### Sprint 2 — RFC Specification Knowledge Base
- `parsers/rfc_fetcher.py`: Fetch and cache IETF RFC text (14 key IAM RFCs)
- `parsers/rfc_scanner.py`: Detect RFC citations in Java comments + semantic inference
- `linker/osgi_resolver.py`: `@Reference` → `RESOLVES_TO` edges
- `Specification` nodes in Neo4j with `IMPLEMENTS_SPEC` edges
- RFC context injected into reduce step (zero extra LLM calls)

### Sprint 3 — MCP Server
- `mcp_server.py`: Four tools: `query_codebase`, `blast_radius`, `audit_spec_compliance`, `recall_session`
- Cursor / Claude Desktop integration via stdio transport
- `blast_radius` and `recall_session` make zero LLM calls

### Sprint 4 — Query Intelligence & Performance
- Query expansion (1 cheap fast-model call for conceptual queries)
- Cross-encoder re-ranking (`cross-encoder/ms-marco-MiniLM-L-6-v2`, zero API cost)
- Multiprocessing Java parser (`ProcessPoolExecutor`)
- Community fingerprint cache (skip LLM if community unchanged)
- Nomic embeddings optional (`nomic-ai/nomic-embed-text-v1.5`, 768-dim, opt-in)
