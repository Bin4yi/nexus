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
| **Local LLM micro-drafts** | Ollama `llm/local_drafting.py` generates EntryPoint summaries at $0 cloud cost |
| **Resilience** | `tenacity` retry decorators on all Neo4j, GDS, and LLM calls |
| **Scale** | ProcessPoolExecutor for parallel parsing; ThreadPoolExecutor for summarisation; APOC batch for Neo4j |
| **Research-grade chunking** | AST-aware sliding window (CHUNK_SIZE=512, OVERLAP=128) with global context prefix on every chunk |
| **GPU-accelerated embeddings** | Optional `FastEmbedder` (nomic-ai/nomic-embed-text-v1.5, 768-dim) with auto ONNX provider selection |
| **OSGi wiring** | `parsers/osgi_parser.py` resolves `@Component`/`@Reference` pairs → `[:RESOLVES_TO]` edges |
| **Specification grounding** | RFC markdown files → `(:Specification)` nodes + `[:IMPLEMENTS_SPEC]` edges |
| **Weighted graph routing** | Dijkstra with CALLS=1.0 / REMOTE_CALLS=5.0 — cross-service hops cost 5× local calls |
| **Azure OpenAI** | `settings.make_llm_client(tier)` factory switches between `openai.OpenAI` and `openai.AzureOpenAI` |
| **Zero-LLM Map step for questions** | `mode=question` converts ChromaDB cosine distance directly to a 0–100 score |

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
                          │ .java, .xml, .toml, .sql files
                          ▼
┌─────────────────────────────────────────────────────────────┐
│             STAGE 2 — Extract (Sprint 1–2)                  │
│  pipeline/ingest.py → ProcessPoolExecutor (parallel)        │
│  • Workers: JavaParser + UIRChunker per CPU core            │
│  • PARSER_FILE_BATCH_SIZE files per worker batch            │
│                                                             │
│  JavaParser (parsers/java_parser.py)                        │
│  • Tree-sitter AST → UIR objects                            │
│  • Method visibility + modifiers (public/static/abstract)   │
│  • Annotations as structured dicts [{"name":"Value",...}]   │
│  • Generic type preservation (List<User> preserved)         │
│  • OSGi lifecycle detection (@Activate → lifecycle_role)    │
│  • Lambda/stream body call extraction                       │
│  • Parameter-level annotations (@QueryParam, @PathVariable) │
│                                                             │
│  OSGiParser (parsers/osgi_parser.py) [Sprint 2]             │
│  • @Component(service=...) → OSGiComponentInfo              │
│  • @Reference field → OSGiResolutionEdge                    │
│                                                             │
│  RFCParser (parsers/rfc_parser.py) [Sprint 4]               │
│  • ./rfcs/*.md → SpecificationInfo objects                  │
│  • // RFC 6749 in Java → SpecImplementsEdge                 │
│                                                             │
│  ConfigurationParser (parsers/config_parser.py)             │
│  • deployment.toml → tomllib (Python 3.11+) with fallback   │
│  • *.xml → element names + ${placeholder} extraction        │
│  • *.properties → key=value pairs                           │
│  • application.yml → top-level YAML keys                    │
│                                                             │
│  SQLSchemaParser (parsers/sql_schema_parser.py)             │
│  • dbscripts/*.sql → CREATE TABLE → DatabaseTableInfo       │
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
│  • JAX-RS: class @Path + method @Path merged                │
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
│  • 19 relationship types via APOC batch                     │
│  • RESOLVES_TO edges (Sprint 2: OSGi wiring)                │
│  • IMPLEMENTS_SPEC edges (Sprint 4: RFC grounding)          │
│                                                             │
│  ChromaDB Embedder (vectorstore/embedder.py)                │
│  ChromaEmbedder: all-MiniLM-L6-v2 (384-dim, CPU, default)  │
│  FastEmbedder: nomic-embed-text-v1.5 (768-dim, GPU opt-in)  │
│                                                             │
│  UIRChunker (vectorstore/chunker.py) [Sprint 1]             │
│  • Sliding window: CHUNK_SIZE=512, OVERLAP=128              │
│  • Every chunk prefixed: [Package: …] [Class: …]           │
│  • code_logic + code_intent collections                     │
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
│  • ThreadPoolExecutor (SUMMARIZER_MAX_WORKERS parallel)     │
│  • → community_summaries ChromaDB collection               │
│                                                             │
│  NodeTagger → :EntryPoint / :DataSink labels               │
│                                                             │
│  FlowExtractor → Weighted GDS Dijkstra [Sprint 3]          │
│  • CALLS weight=1.0, REMOTE_CALLS weight=5.0               │
│  • maxDepth=15 enforced                                    │
│  • → FlowPath objects (enriched with config + tables)      │
│                                                             │
│  LocalDraftingEngine (llm/local_drafting.py) [Sprint 4]    │
│  • Ollama → 3-sentence micro_draft per EntryPoint class    │
│  • Stored as micro_draft property in Neo4j ($0 cost)       │
│                                                             │
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
| `Component` | `geid`, `fqn`, `kind`, `docstring`, `visibility`, `is_abstract`, `is_final`, `annotations` (JSON), `community_id`, `micro_draft` |
| `LogicUnit` | `geid`, `fqn`, `kind`, `body_text`, `docstring`, `visibility`, `is_static`, `is_abstract`, `is_final`, `is_synchronized`, `lifecycle_role`, `annotations` (JSON), `community_id` |
| `AnnotationType` | `name` |
| `ExceptionType` | `fqn` |
| `EventClass` | `fqn` |
| `DatabaseTable` | `name`, `repo_name`, `source_file` |
| `Configuration` | `config_key`, `config_type`, `source_file`, `repo_name` |
| `Specification` | `rfc_number`, `title`, `source_file` |

### Relationship Types (19)

| Type | From → To | Sprint | Meaning |
|---|---|---|---|
| `CONTAINS` | Project → Module | Core | Repository containment |
| `DECLARES` | Module → Component | Core | Maven artifact contains class |
| `HAS_METHOD` | Component → LogicUnit | Core | Class contains method |
| `CALLS` | LogicUnit → LogicUnit | Core | Direct method call (weight=1.0) |
| `IMPLEMENTS` | Component → Component | Core | Interface implementation |
| `EXTENDS` | Component → Component | Core | Class inheritance |
| `DEPENDS_ON` | Module → Module | Core | Maven dependency |
| `INJECTS` | Component → Component | Core | DI injection (`@Autowired`, `@Reference`) |
| `REMOTE_CALLS` | LogicUnit → LogicUnit | Core | Cross-service REST call (weight=5.0) |
| `THROWS` | LogicUnit → ExceptionType | Core | Exception declaration |
| `OVERRIDES` | LogicUnit → LogicUnit | Core | Method override |
| `INSTANTIATES` | LogicUnit → Component | Core | `new X()` creation |
| `HANDLES_EVENT` | Component → EventClass | Core | WSO2 event handler |
| `ANNOTATED_WITH` | Component/LogicUnit → AnnotationType | Core | Annotation usage |
| `RETURNS` / `RECEIVES` | LogicUnit → Component | Core | Return/param type |
| `QUERIES_TABLE` | Component → DatabaseTable | S2 | DAO accesses table |
| `READS_CONFIG` | Component → Configuration | S2 | Class reads config key |
| `RESOLVES_TO` | Component → Component | S2 | OSGi service resolution (interface → implementation) |
| `IMPLEMENTS_SPEC` | Component → Specification | S4 | Code implements IETF RFC |

---

## ChromaDB Collections

| Collection | Document | Metadata | Use Case |
|---|---|---|---|
| `code_logic` | Method body text (sliding window chunks) | `geid`, `fqn`, `chunk_type`, `file_path`, `start_line`, `repo_name` | "Find code that does X" |
| `code_intent` | Javadoc description (one per method) | `geid`, `fqn`, `chunk_type`, `file_path` | "Find code intended for X" |
| `community_summaries` | LLM community summary | `community_id`, `node_count`, `llm_model` | Route C map step |
| `flow_narratives` | End-to-end flow story | `entry_point_fqn`, `data_sink_fqn`, `path_length` | Architecture trace questions |
| `l2_subsystem_summaries` | Domain summary | `domain`, `community_count` | Sub-system overviews |
| `l3_global_architecture` | Global arch document | `generated_at`, `total_communities` | Route D global queries |

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

---

## Chunking Architecture (Sprint 1)

```
LogicUnit (method body)
      │
      ▼  _build_context_prefix(fqn)
[Package: org.wso2.identity] [Class: AuthzEndpoint]
      │
      ▼  count_tokens(full_text)
  ≤ 512 tokens?          > 512 tokens?
      │                        │
      │                        ▼  _sliding_window(text, 512, 128)
      │                   Window 0: tokens[0:512]
      │                   Window 1: tokens[384:896]   (overlap=128)
      │                   Window 2: tokens[768:1280]
      │                        │
      └──────────┬─────────────┘
                 ▼
         EmbeddingChunk objects
         chunk_id: {geid}_code_logic[_{i}]
```

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

---

## OSGi Resolution Architecture (Sprint 2)

```
@Component(service={OAuthService.class})
class OAuthServiceImpl implements OAuthService { ... }
                    │
                    │  OSGiParser.parse_components()
                    ▼
          OSGiComponentInfo(fqn="...OAuthServiceImpl",
                            service_interfaces=["OAuthService"])
                    │
@Reference          │  OSGiParser.build_resolution_edges()
OAuthService svc;   │
                    ▼
    OSGiResolutionEdge(interface_fqn="...OAuthService",
                       implementation_fqn="...OAuthServiceImpl",
                       reference_field="svc")
                    │
                    ▼  loader.load_resolves_to_edges()
    (OAuthService:Component)-[:RESOLVES_TO]->(OAuthServiceImpl:Component)
```

---

## Weighted Dijkstra Architecture (Sprint 3)

```
GDS Flow Graph Projection:
  CALLS        { defaultValue: 1.0 }   ← cheap: same process
  REMOTE_CALLS { defaultValue: 5.0 }   ← expensive: network boundary
  INJECTS      { defaultValue: 1.0 }
  RESOLVES_TO  { defaultValue: 1.0 }
  ...

gds.shortestPath.dijkstra.stream(graph, {
    sourceNode: entryPoint,
    targetNode: dataSink,
    relationshipWeightProperty: 'weight'
})

Result: paths that stay within a service are preferred over paths
        that cross service boundaries via REST calls.
```

---

## Two-Tier + Local LLM Strategy

| Tier | Model | Cost | Where Used |
|---|---|---|---|
| **Fast (cloud)** | `gpt-4o-mini` | ~$0.005/call | Community summarisation, map scoring, flow narratives, L2 rollup |
| **Strong (cloud)** | `gpt-4o` | ~$0.05/call | Final reduce answer, L3 global rollup only |
| **Local (Ollama)** | `llama3.2:3b` | **$0** | EntryPoint micro-draft generation (opt-in) |

All cloud callers use `settings.make_llm_client(tier="fast"/"strong")`.
Switching between OpenAI and Azure requires only `.env` changes.
