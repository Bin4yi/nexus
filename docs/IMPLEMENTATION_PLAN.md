# CodeNexus — Implementation Plan (v1 + v2)

> **Document Version**: 2.0
> **Last Updated**: March 2026
> **Scope**: Comprehensive plan covering Phase 01 (complete), Phase 02 (complete), and v2 Sprint roadmap

---

## 1. Overview

This document tracks every implementation milestone for CodeNexus — from the initial
Phase 01 Knowledge Base Engine through the v2 accuracy improvements and planned future sprints.

> See [ARCHITECTURE.md](ARCHITECTURE.md) for system diagrams and [PROJECT_PROPOSAL.md](PROJECT_PROPOSAL.md) for project context.

---

## 2. Phase 01 — Knowledge Base Engine ✅ Complete

### Component 1: Infrastructure & Configuration

#### ✅ `docker-compose.yml`

Three-service infrastructure:

| Service | Image | Ports | Purpose |
|---|---|---|---|
| `neo4j` | `neo4j:5.x` + APOC + GDS | 7474 / 7687 | Structural graph + Leiden + Dijkstra |
| `chromadb` | `ghcr.io/chroma-core/chroma:0.4.15` | 8000 | Semantic vector store |
| `redis` | `redis:7` | 6379 | Pipeline state |

Key configuration: APOC + GDS plugins, persistent volumes, health checks, shared `codenexus` network.

#### ✅ `requirements.txt`

All production dependencies with version constraints:

| Package | Version | Purpose |
|---|---|---|
| `pydantic` | ≥2.0,<3.0 | Data models |
| `pydantic-settings` | ≥2.0,<3.0 | `.env` loading |
| `tree-sitter` | ≥0.20 | Java AST parsing |
| `tree-sitter-java` | ≥0.20 | Java grammar |
| `neo4j` | ≥5.0,<6.0 | Graph database driver |
| `chromadb` | ≥0.4.0,<0.6.0 | Vector store client |
| `sentence-transformers` | ≥2.0 | Local embedding model |
| `redis` | ≥4.0,<6.0 | State tracking |
| `openai` | ≥1.0,<2.0 | LLM API (OpenAI + Azure) |
| `lxml` | ≥4.9 | Maven pom.xml parsing |
| `tiktoken` | ≥0.5 | Token counting |
| `tenacity` | ≥8.0,<10.0 | Retry logic |
| `gitpython` | ≥3.1 | Git clone/pull |
| `pyyaml` | ≥6.0 | repos.yaml parsing |
| `click` | ≥8.0 | CLI framework |
| `pytest` | ≥7.0 | Test runner |
| `pytest-timeout` | ≥2.0 | Test timeouts |

#### ✅ `config/settings.py`

Pydantic `BaseSettings` with all operational constants. All modules import the `settings` singleton.

---

### Component 2: Java AST Parser ✅

#### ✅ `parsers/uir.py` — Universal Intermediate Representation

Pydantic models forming the data contract between parsers and all downstream components:

```
Project → Module → Component → LogicUnit
```

| Model | Key Fields |
|---|---|
| `Parameter` | `name`, `type_name`, `doc`, `annotations: list[dict]` |
| `FieldDeclaration` | `name`, `type_name`, `annotations: list[str]`, `is_injected: bool` |
| `LogicUnit` | `geid`, `fqn`, `kind`, `visibility`, `is_static`, `is_abstract`, `is_final`, `is_synchronized`, `lifecycle_role`, `annotations: list[dict]`, `calls`, `throws`, `overrides`, `instantiates` |
| `Component` | `geid`, `fqn`, `kind`, `visibility`, `is_abstract`, `is_final`, `annotations: list[dict]`, `implements`, `extends`, `logic_units`, `fields` |
| `Module` | `geid`, `name`, `components`, `dependencies` |
| `Project` | `geid`, `name`, `url`, `branch`, `modules` |

GEID generation: `sha256(f"{repo_name}::{fqn}")[:16]`

#### ✅ `parsers/java_parser.py` — Java AST Parser (Tree-sitter)

| Extraction | Tree-sitter pattern | UIR field |
|---|---|---|
| Class declarations | `class_declaration name: (identifier)` | `Component(kind="class")` |
| Interface declarations | `interface_declaration` | `Component(kind="interface")` |
| Method declarations | `method_declaration` | `LogicUnit(kind="method")` |
| Modifiers | `modifiers` child node | `visibility`, `is_static`, `is_abstract`, `is_final`, `is_synchronized` |
| Annotations (structured) | `marker_annotation`, `annotation` → `_parse_annotation()` | `annotations: list[dict]` |
| Parameter annotations | `annotation` on `formal_parameter` | `Parameter.annotations` |
| OSGi lifecycle | `@Activate` / `@Deactivate` / `@Modified` | `lifecycle_role` |
| Method invocations | `method_invocation` + lambda recursion | `calls[]` |
| Call targets | `method_invocation name:` | `LogicUnit.calls[]` |
| Inheritance | `superclass (type_identifier)` | `Component.extends` |
| Implementations | `super_interfaces (type_list)` | `Component.implements[]` |
| Generic types | `generic_type` nodes | Preserved in `type_name` fields |

#### ✅ `parsers/javadoc_parser.py`

Extracts `@param`, `@return`, `@throws`, `@see`, `@deprecated` from `/** ... */` blocks.
Produces `docstring` and `format_for_embedding()` for clean ChromaDB vectors.

---

### Component 3: Neo4j Graph Schema & Loader ✅

#### ✅ `graph/schema.py`

Creates all constraints and indexes on startup (idempotent via `IF NOT EXISTS`).

Constraints (10): `Project`, `Module`, `Component`, `LogicUnit`, `AnnotationType`,
`ExceptionType`, `EventClass`, `DatabaseTable`, `Configuration`, `Specification`

Indexes (14): FQN lookups, community queries, return type, event handler, kind filtering,
DB table/config type, visibility (v2), lifecycle (v2), spec RFC (v2 Sprint 2 prep)

#### ✅ `graph/loader.py`

Three-phase bulk loading (idempotent MERGE):

**Phase 1 — Nodes:**
```cypher
UNWIND $batch AS item
MERGE (n:LogicUnit {geid: item.geid})
SET n.fqn = item.fqn, n.visibility = item.visibility,
    n.is_static = item.is_static, n.lifecycle_role = item.lifecycle_role,
    n.annotations = item.annotations  -- JSON string
```

**Phase 2 — Hierarchy edges** (CONTAINS, DECLARES, HAS_METHOD)

**Phase 3 — Cross-reference edges** via APOC batch:
```cypher
CALL apoc.periodic.iterate(
  'MATCH (a:LogicUnit) WHERE size(a._calls_fqn) > 0 RETURN a',
  'UNWIND a._calls_fqn AS fqn MATCH (b:LogicUnit {fqn: fqn}) MERGE (a)-[:CALLS]->(b)',
  {batchSize: 500}
)
```

All writes call `.consume()` to prevent Neo4j lazy-execution silent drops.

---

### Component 4: ChromaDB Semantic Store ✅

#### ✅ `vectorstore/chunker.py`

Functional chunking — one method = one chunk (never character-count splits):
- `code_logic` chunk: method signature + body
- `code_intent` chunk: parsed Javadoc (if present)

#### ✅ `vectorstore/embedder.py`

- Uses `all-MiniLM-L6-v2` (384-dimensional, CPU-only)
- Two collections: `code_logic` + `code_intent`
- Batch upsert with configurable `EMBEDDING_BATCH_SIZE`
- Automatic batch-splitting retry for large payloads

---

### Component 5: Cross-Repo Linker ✅

#### ✅ `linker/maven_resolver.py`

Reads `pom.xml` → extracts `<dependency>` elements → `DEPENDS_ON` edges between Maven modules.
Builds global module registry for cross-repo dependency resolution.

#### ✅ `linker/api_bridge.py`

Two-pass REST bridge detection:
1. **Pass 1**: Register all Spring + JAX-RS endpoints (with class-level + method-level path composition)
2. **Pass 2**: Detect HTTP client calls, match against registry → `REMOTE_CALLS` edges

---

### Component 6: Ingestion Pipeline & CLI ✅

#### ✅ `pipeline/mirror.py`

Reads `repos.yaml`, runs `git clone` (first time) or `git pull` (subsequent).

#### ✅ `pipeline/orchestrator.py`

Complete pipeline conductor. Runs all stages sequentially with Redis state tracking.

#### ✅ `pipeline/cli.py`

Click-based CLI: `nexus ingest`, `nexus update`, `nexus validate`, `nexus stats`, `nexus analyze-pr`

---

## 3. Phase 02 — End-to-End Flow Extraction & Global Rollup ✅ Complete

### Task 1 — SQL Schema & Configuration Parsers ✅

#### ✅ `parsers/sql_schema_parser.py`

Scans `dbscripts/` folders for `CREATE TABLE` DDL → `DatabaseTable` nodes + `QUERIES_TABLE` edges.

#### ✅ `parsers/config_parser.py`

Parses WSO2 configuration files:
- `deployment.toml` — `tomllib` (Python 3.11+) with regex fallback
- `*.xml` — element names + `${placeholder}` extraction
- `*.properties` — `key=value` and `key: value` patterns
- `application.yml` — top-level YAML keys

### Task 2 — EntryPoint / DataSink Tagging ✅

#### ✅ `graph/tagger.py`

- `:EntryPoint` → `@RequestMapping`, `@Path`, `HttpServlet`, Controller/Servlet class patterns
- `:DataSink` → `@Repository`, DAO patterns, `QUERIES_TABLE` edges, SQL method calls

### Task 3 — GDS Dijkstra Flow Extraction ✅

#### ✅ `graph/flow_extractor.py`

- Projects execution-flow edges into GDS graph
- Runs `gds.shortestPath.dijkstra.stream` for each (EntryPoint, DataSink) pair
- Returns `FlowPath` objects enriched with config keys and table names

### Task 4 — Flow Narrative Generation ✅

#### ✅ `reasoning/flow_summarizer.py`

- Parallel LLM calls via `ThreadPoolExecutor` (fast model)
- Stores in ChromaDB `flow_narratives`

### Task 5 — Global GraphRAG Rollup ✅

#### ✅ `community/global_rollup.py`

```
L1: Leiden Community Summaries (individual clusters)
L2: Sub-System Summaries (11 domains, fast model)
L3: Global Architecture Document (strong model, single call)
```

---

## 4. v2 Sprint 1 — WSO2-IS Parser Accuracy ✅ Complete

### 1.1 Method Modifiers + Visibility ✅

Added to `parsers/uir.py`:
```python
# LogicUnit
visibility: str = "package"       # "public"|"protected"|"private"|"package"
is_static: bool = False
is_abstract: bool = False
is_final: bool = False
is_synchronized: bool = False

# Component
visibility: str = "public"
is_abstract: bool = False
is_final: bool = False
```

Extraction via `modifiers` tree-sitter child node in `parsers/java_parser.py`.
Written to Neo4j in `graph/loader.py` with new indexes in `graph/schema.py`.

### 1.2 Full Annotation Value Parsing ✅

Changed `annotations: list[str]` → `annotations: list[dict]` in `Component` and `LogicUnit`.

New `_parse_annotation(node, source) → dict` in `parsers/java_parser.py`:
- `marker_annotation` → `{"name": "Override"}`
- Single-value → `{"name": "Value", "value": "${server.host}"}`
- Named-attr → `{"name": "Reference", "cardinality": "MANDATORY"}`

Stored in Neo4j as `json.dumps(annotations)`. Updated `load_annotated_with` in `graph/loader.py`.

### 1.3 Parameter Annotation Extraction ✅

Added `annotations: list[dict]` to `Parameter` in `parsers/uir.py`.
Updated `_extract_parameters()` in `parsers/java_parser.py` to extract annotations
on each `formal_parameter` node.

Enables: `@QueryParam("client_id") String clientId` → `Parameter(name="clientId", annotations=[{"name":"QueryParam","value":"client_id"}])`

### 1.4 Generic Type Preservation ✅

`parsers/java_parser.py` now preserves generic types in `type_name` fields.
Generics are stripped only for FQN-based graph MERGE keys via `_type_for_graph_key()`.

### 1.5 Lambda / Stream Call Extraction ✅

`_walk_calls()` in `parsers/java_parser.py` now recurses into lambda expression bodies,
capturing calls inside `.stream().filter(x -> x.method())` chains.

### 1.6 JAX-RS Path Composition + API Contracts ✅

`linker/api_bridge.py` now:
- Extracts class-level `@Path` before the first `{` in the source
- Merges with method-level `@Path`: `effective_path = normalize(class_base + "/" + method_path)`
- Extracts `@Consumes` / `@Produces` into `EndpointRegistration`

### 1.7 OSGi Lifecycle + Config Parser Accuracy ✅

**OSGi lifecycle**: `parsers/java_parser.py` detects `@Activate`, `@Deactivate`, `@Modified`
→ sets `lifecycle_role` on `LogicUnit`. New index `logicunit_lifecycle` in `graph/schema.py`.

**Config parser fixes** in `parsers/config_parser.py`:
- TOML: replaced flat regex with `tomllib.load()` + `_flatten_toml_dict()` recursion
- XML attributes: `_parse_xml_config()` already extracts element names + placeholders
- Properties files: new `_scan_properties_file()` for `*.properties` files

---

## 5. v2 Sprint 2 — RFC Specification Knowledge Base (Planned)

### Files to Create
| File | Purpose |
|---|---|
| `parsers/rfc_fetcher.py` | Fetch + cache IETF RFC text (14 key IAM RFCs) |
| `parsers/rfc_scanner.py` | Detect RFC citations in Java code + semantic inference |
| `linker/osgi_resolver.py` | `@Reference` + `implements` index → `RESOLVES_TO` edges |

### Files to Modify
| File | Changes |
|---|---|
| `graph/schema.py` | `Specification.spec_id` constraint already added (Sprint 1 prep) |
| `graph/loader.py` | `load_specifications()`, `load_spec_edges()`, `load_resolves_to_edges()` |
| `pipeline/orchestrator.py` | Wire in rfc_fetcher, rfc_scanner, osgi_resolver |
| `reasoning/reduce_step.py` | RFC evidence block in reduce prompt (zero extra LLM calls) |
| `reasoning/graph_retriever.py` | `get_spec_sections(fqns)` |
| `config/settings.py` | `RFC_CACHE_DIR`, `RFC_OFFLINE_MODE` |

### Key IAM RFCs to Index

| RFC | Title |
|---|---|
| 6749 | OAuth 2.0 Authorization Framework |
| 6750 | Bearer Token Usage |
| 7519 | JSON Web Token (JWT) |
| 7521 | Assertion Framework for OAuth 2.0 |
| 7636 | PKCE for OAuth Public Clients |
| 7591 | OAuth 2.0 Dynamic Client Registration |
| 8693 | OAuth 2.0 Token Exchange |
| 9068 | JWT Profile for OAuth 2.0 Access Tokens |
| 7642–7644 | SCIM Definitions, Core Schema, Protocol |

---

## 6. v2 Sprint 3 — MCP Server (Planned)

### Files to Create
| File | Purpose |
|---|---|
| `mcp_server.py` | MCP server with 4 tools for Cursor/Claude Desktop |

### Four MCP Tools

| Tool | LLM calls | Description |
|---|---|---|
| `query_codebase(question)` | 1 (strong model) | Full GraphRAG pipeline — answer any question |
| `blast_radius(fqn, depth)` | 0 | Graph traversal only — impact analysis |
| `audit_spec_compliance(fqn)` | 1 (strong model) | RFC compliance check with citation |
| `recall_session(files_touched)` | 0 | Community summaries for developer orientation |

### Cursor Configuration

```json
// ~/.cursor/mcp.json
{
  "nexus": {
    "command": "py",
    "args": ["/path/to/nexus/mcp_server.py"],
    "env": {"PYTHONPATH": "/path/to/nexus"}
  }
}
```

---

## 7. v2 Sprint 4 — Query Intelligence & Performance (Planned)

### 4.1 Two-Tier GPT Wiring ✅ (done in Sprint 1)
All callers updated in `config/settings.py`. Community summarizer, map step, reduce step,
and global rollup all call the correct model tier.

### 4.2 Community Summary Fingerprint Cache
`community/summarizer.py`: before calling LLM, compute `fqn_hash = sha256(sorted(fqns))[:12]`.
Check ChromaDB for existing summary with same hash. Skip if unchanged.

Effect: re-ingest after a 2-file PR costs ~$0 for summarisation (90%+ communities unchanged).

### 4.3 Query Expansion
`reasoning/router.py`: for conceptual queries, call fast model to generate 2 paraphrased
sub-queries, merge results, dedup by community_id. Cost: ~$0.0001 per query.

### 4.4 Cross-Encoder Re-Ranking (Free)
`reasoning/map_step.py`: after cosine retrieval, re-rank with
`cross-encoder/ms-marco-MiniLM-L-6-v2` (CPU, no API cost). Uses `sentence-transformers`
which is already a dependency.

### 4.5 Multiprocessing File Parsing
`pipeline/_worker.py` + `pipeline/orchestrator.py`: `ProcessPoolExecutor` for parallel
Java file parsing. Worker instantiates `JavaParser` inside the subprocess (tree-sitter C
bindings don't pickle — cannot instantiate in the main process and share).

### 4.6 Nomic Embeddings (Optional)
`vectorstore/embedder.py`: opt-in `EMBEDDING_BACKEND=nomic` → `nomic-ai/nomic-embed-text-v1.5`
(768-dim, better quality). Stored in separate collections (`code_logic_nomic`, `code_intent_nomic`)
to avoid dimension collision. Requires `pip install nexus[nomic]`.

---

## 8. Verification Queries

### Sprint 1 — Parser Accuracy
```bash
# Parse phase only
py main.py ingest --dry-run
```

```cypher
-- Verify visibility is populated
MATCH (n:LogicUnit) WHERE n.visibility IS NOT NULL RETURN count(n)
-- Expected: > 0

-- Verify annotation dict format
MATCH (n:LogicUnit) WHERE n.annotations CONTAINS '"name": "QueryParam"' RETURN n.fqn LIMIT 5
-- Expected: JAX-RS methods with @QueryParam data

-- Verify OSGi lifecycle
MATCH (n:LogicUnit) WHERE n.lifecycle_role = 'activate' RETURN n.fqn LIMIT 5
-- Expected: OSGi @Activate methods

-- Verify public API methods
MATCH (n:LogicUnit {visibility: 'public', is_static: false}) RETURN count(n)
```

### Sprint 2 — RFC Knowledge Base
```cypher
-- Verify Specification nodes created
MATCH (s:Specification) RETURN count(s)
-- Expected: 200-600

-- Verify IMPLEMENTS_SPEC edges
MATCH ()-[r:IMPLEMENTS_SPEC]->() RETURN count(r)
-- Expected: > 0
```

```bash
py main.py query "does the PKCE implementation comply with RFC 7636 section 4.2?"
# Expected: answer cites actual RFC 7636 §4.2 text
```

### Sprint 3 — MCP Server
```bash
py mcp_server.py &
# Expected: "CodeNexus MCP server running on stdio"
# In Cursor: call blast_radius("com.wso2.carbon.identity.oauth.OAuthAdminService")
```

### Sprint 4 — Performance
```bash
# Second ingest after 2 file changes:
# Log should show "Community X unchanged — skipping LLM call" for 90%+ of communities
# Log should show "model=gpt-4o-mini" for summarization, "model=gpt-4o" for reduce
```

---

## 9. What Is NOT Being Built

| Dropped Idea | Reason |
|---|---|
| Ollama / local LLM | GPT models only — simpler, no GPU requirement |
| Full RFC markdown parser (200KB per RFC) | Section-only extraction gives 90% value at 5% complexity |
| "Atomic Neo4j+ChromaDB transactions" | Architecturally impossible; WAL + compensating actions is correct approach |
| Groovy/Kotlin parsers | Out of scope for WSO2-IS v2 |
| Embedding fine-tuning | Ops burden exceeds value at current stage |
| Rebuild orchestrator as `pipeline/ingest.py` | `orchestrator.py` is production-grade; rename breaks all tests |
