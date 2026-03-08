# CodeNexus — Code Reference

Per-file documentation for all source modules. Organized by layer (bottom-up, from data models to entry point).

---

## `main.py` — CLI Entry Point

**Purpose**: The single entry point for all CodeNexus operations.

### Commands
```bash
python main.py ingest   # Run the full ingestion pipeline
```

### Functions
| Function | Description |
|---|---|
| `ingest()` | Instantiates `IngestionPipeline`, calls `.run()`, logs all stats. Exits with code 1 if any repos failed. |
| `main()` | `argparse` router — parses the subcommand and dispatches to the appropriate function. |

---

## `config/settings.py` — Central Configuration

**Purpose**: Loads all environment variables from `.env` into a typed `Settings` Pydantic model. All modules import `settings` from here — nothing else reads `.env` directly.

### Key Settings
| Setting | Env Var | Default | Description |
|---|---|---|---|
| `neo4j_uri` | `NEO4J_URI` | `bolt://localhost:7687` | Neo4j connection |
| `neo4j_auth` | `NEO4J_USER` + `NEO4J_PASSWORD` | — | Returns `(user, pass)` tuple |
| `chroma_host` | `CHROMA_HOST` | `localhost` | ChromaDB host |
| `chroma_port` | `CHROMA_PORT` | `8000` | ChromaDB port |
| `llm_provider` | `LLM_PROVIDER` | `openai` | `openai` or `azure` — selects client type in `make_llm_client()` |
| `llm_api_key` | `LLM_API_KEY` | — | API key for OpenAI or Azure OpenAI |
| `llm_model` | `LLM_MODEL` | `gpt-5` | Model for all LLM calls |
| `llm_azure_endpoint` | `LLM_AZURE_ENDPOINT` | `""` | Azure OpenAI endpoint URL |
| `llm_azure_api_version` | `LLM_AZURE_API_VERSION` | `2025-04-01-preview` | Azure API version |
| `llm_deployment` | `LLM_DEPLOYMENT` | `""` | Azure deployment name (usually same as model) |
| `gds_graph_name` | `GDS_GRAPH_NAME` | `codenexus-graph` | GDS in-memory graph name |
| `max_context_tokens` | `MAX_CONTEXT_TOKENS` | `32000` | Token budget ceiling for reasoning LLM calls |
| `community_summarization_enabled` | `COMMUNITY_SUMMARIZATION_ENABLED` | `true` | Toggle LLM summarization |

### `make_llm_client()` — LLM Factory Method

All modules that need an LLM client call `settings.make_llm_client()` instead of constructing `OpenAI(...)` directly.  This ensures a single location controls the provider switch:

```python
def make_llm_client(self):
    if self.llm_provider.lower() == "azure":
        from openai import AzureOpenAI
        return AzureOpenAI(
            api_key=self.llm_api_key,
            azure_endpoint=self.llm_azure_endpoint,
            api_version=self.llm_azure_api_version,
        )
    from openai import OpenAI
    return OpenAI(api_key=self.llm_api_key)
```

> **GPT-5 / Reasoning model notes**: GPT-5 does not accept `temperature` or `max_tokens` parameters.  Use `max_completion_tokens` only.  Internal thinking tokens count against this budget, so values < 1 000 may result in empty `content`.  Minimum recommended values: 1 800 (reduce/capability), 3 000 (search term extraction), 6 000 (narrative).

---

## `parsers/uir.py` — Universal Intermediate Representation

**Purpose**: Pydantic models that define the **language-agnostic data contract** between parsers and all downstream components. The entire pipeline flows data through these models.

### Models

#### `Parameter`
A single method/constructor parameter. Fields: `name`, `type_name`, `doc` (from `@param` Javadoc).

#### `FieldDeclaration`
A field declared in a class body.
- `name`, `type_name`: The field's identifier and declared type
- `annotations`: List of annotation strings on the field
- `is_injected`: `True` if annotated with `@Autowired`, `@Inject`, `@Resource`, or `@Reference` — signals a `INJECTS` edge

#### `LogicUnit`
The **atomic semantic unit** — a single Java method or constructor. Maps to a Neo4j `:LogicUnit` node and two ChromaDB vectors.

| Field | Description |
|---|---|
| `geid` | SHA256(repo::fqn)[:16] — the cross-DB bridge key |
| `fqn` | Fully qualified name: `com.example.Service.getUser` |
| `kind` | `"method"` or `"constructor"` |
| `parameters` | List of `Parameter` |
| `return_type` | Java return type string |
| `body_text` | Raw source body for embedding |
| `docstring` | Javadoc description block |
| `annotations` | List of annotations (e.g. `["@Override", "@Transactional"]`) |
| `calls` | FQNs of directly called methods → `CALLS` edges |
| `throws` | Exception type names from `throws` clause → `THROWS` edges |
| `overrides` | Parent method FQN if `@Override` detected → `OVERRIDES` edge |
| `instantiates` | Class names from `new X()` expressions → `INSTANTIATES` edges |

#### `Component`
A Java class, interface, enum, or annotation type. Maps to a Neo4j `:Component` node.

| Field | Description |
|---|---|
| `geid` | Cross-DB bridge key |
| `fqn` | `com.example.auth.UserService` |
| `kind` | `"class"`, `"interface"`, `"enum"`, `"annotation"` |
| `implements` | List of interface FQNs → `IMPLEMENTS` edges |
| `extends` | Parent class FQN → `EXTENDS` edge |
| `logic_units` | Nested `LogicUnit` objects |
| `fields` | `FieldDeclaration` objects → `HAS_FIELD` + `INJECTS` edges |
| `annotations` | Class-level annotations → `ANNOTATED_WITH` edges |
| `is_event_handler` | `True` if extends `AbstractEventHandler` or implements `EventHandler` |

#### `Module`
A Maven artifact (one `pom.xml`). Contains `components` and `dependencies` (→ `DEPENDS_ON` edges).

#### `Project`
A Git repository. Contains `modules`. Top of the containment hierarchy.

---

## `parsers/java_parser.py` — Java AST Parser

**Purpose**: Parses Java source files into UIR objects using the Tree-sitter parser. The single most complex component in the system — all structural and semantic knowledge extraction happens here.

### Class: `JavaParser`

#### `parse_file(file_path, repo_name) → list[Component]`
Main entry point. Parses one `.java` file and returns all top-level and nested `Component` objects with their `LogicUnit` children.

**Algorithm:**
1. Parse bytes → Tree-sitter syntax tree
2. Extract package declaration and import statements
3. Walk `class_declaration`, `interface_declaration`, `enum_declaration` nodes
4. For each type: extract modifiers, superclass, interfaces, body

#### What Each Extraction Method Does

| Method | Extracts | Graph Edge |
|---|---|---|
| `_extract_package` | Package name string | (context) |
| `_extract_imports` | `{simple_name: fqn}` map for type resolution | (context) |
| `_extract_extends` | Parent class FQN | `EXTENDS` |
| `_extract_implements` | All implemented interface FQNs (handles `type_list` multi-interface) | `IMPLEMENTS` |
| `_extract_parameters` | Method parameters with types | `RECEIVES` signal |
| `_extract_return_type` | Return type string | `RETURNS` signal |
| `_extract_calls` | All `method_invocation` nodes, best-effort FQN resolution | `CALLS` |
| `_extract_throws` | Exception types from `throws` clause | `THROWS` |
| `_extract_instantiations` | Class names from `object_creation_expression` nodes | `INSTANTIATES` |
| `_extract_fields` | All `field_declaration` nodes; sets `is_injected=True` if DI annotation present | `HAS_FIELD`, `INJECTS` |
| `_extract_annotations` | `marker_annotation` and `annotation` nodes | `ANNOTATED_WITH` |
| `_is_event_handler` | Checks `extends`/`implements` against WSO2 event base types | `HANDLES_EVENT` |

#### `@Override` Detection
If a method has `@Override` in its annotations and the class has a known `extends` FQN, the parser constructs an `overrides` string as `{parent_fqn}.{method_name}` which the loader resolves to a `OVERRIDES` edge.

---

## `parsers/sql_schema_parser.py` — SQL DDL Parser (Phase 2)

**Purpose**: Scans `dbscripts/` folders in mirrored repositories for `CREATE TABLE` DDL statements. Creates `DatabaseTable` nodes in Neo4j and links DAO classes via `QUERIES_TABLE` edges.

### Class: `SQLSchemaParser`

#### `scan_repo(repo_path, repo_name) → tuple[list[DatabaseTableInfo], list[TableQueryEdge]]`
Scans all `.sql` files under `dbscripts/` directories. Returns:
- `DatabaseTableInfo` objects: `{name, repo_name, file_path, columns}` → `DatabaseTable` nodes
- `TableQueryEdge` objects: `{component_fqn, table_name}` → `QUERIES_TABLE` edges

#### Detection heuristics
- **Table extraction**: Regex matches `CREATE TABLE [IF NOT EXISTS] [schema.]table_name`
- **DAO detection**: Scans Java files for class names matching DAO/Repository patterns and SQL execution method calls (`executeQuery`, `PreparedStatement`, etc.)

---

## `parsers/config_parser.py` — Configuration File Parser (Phase 2)

**Purpose**: Parses WSO2 `deployment.toml`, `repository/conf/*.xml`, and `application.yml` files. Creates `Configuration` nodes and `READS_CONFIG` edges.

### Class: `ConfigurationParser`

#### `scan_repo(repo_path, repo_name) → tuple[list[ConfigurationInfo], list[ConfigReadEdge]]`
Returns:
- `ConfigurationInfo` objects: `{config_key, config_type, source_file, repo_name}` → `Configuration` nodes
- `ConfigReadEdge` objects: `{component_fqn, config_key}` → `READS_CONFIG` edges

#### Supported file types
| Format | Key extraction strategy |
|---|---|
| `deployment.toml` | `[section.subsection]` headings + `key=value` pairs |
| `*.xml` | Element names + `${property.name}` placeholders |
| `*.yml` / `*.yaml` | Top-level YAML keys (indentation=0) |

---

## `parsers/geid.py` — GEID Generator

**Purpose**: Generates the **Global Entity Identifier** — a 16-character hex string that is the primary key shared between Neo4j and ChromaDB.

```python
geid = SHA256(f"{repo_name}::{fqn}").hexdigest()[:16]
```

**Properties**:
- Deterministic: same input always produces same GEID
- Unique per entity: FQN uniqueness within a repo guarantees GEID uniqueness
- Repository-scoped: two repos with the same class name get different GEIDs

---

## `parsers/fqn_builder.py` — FQN Construction

**Purpose**: Builds fully qualified Java names from parsed components. Centralised here so the parser doesn't contain string formatting logic.

| Function | Output |
|---|---|
| `build_component_fqn(package, name)` | `com.example.UserService` |
| `build_logic_unit_fqn(pkg, cls, method, param_types)` | `com.example.UserService.getUser(String,int)` |
| `build_call_fqn(receiver, method, resolved_fqn)` | Best-effort FQN for a call site |

---

## `parsers/javadoc_parser.py` — Javadoc Parser

**Purpose**: Extracts structured data from raw `/** ... */` comment blocks for populating `LogicUnit.docstring`, `return_doc`, `throws_doc`, and parameter descriptions.

### `JavadocParser.parse(raw_comment) → dict`
Returns:
```python
{
    "description": "Main narrative text",
    "params": {"paramName": "description"},
    "return": "return value description",
    "throws": ["IOException if file not found"],
    "see": ["OtherClass#method"],
    "deprecated": True/False
}
```

### `JavadocParser.format_for_embedding(parsed) → str`
Converts the parsed dict to a flat text string optimised for semantic embedding (no `@tag` noise).

---

## `linker/maven_resolver.py` — Maven Dependency Resolver

**Purpose**: Reads `pom.xml` files to extract module identity and Maven dependency declarations, which become `DEPENDS_ON` edges in the graph.

### `MavenResolver`

#### `find_poms(repo_path) → list[Path]`
Recursively finds all `pom.xml` files in a repository, sorted shallow-first (root pom first).

#### `parse_pom(pom_path) → tuple[dict, list[DependencyEdge]]`
- Returns module identity dict: `{group_id, artifact_id, version}`
- Returns list of `DependencyEdge` objects for each `<dependency>` block
- Handles `<scope>` — test-scoped dependencies are included but marked

---

## `linker/api_bridge.py` — REST API Bridge Detector

**Purpose**: Bridges the `CALLS` gap between microservices. Java services rarely call each other over gRPC in-process — they use REST. This two-pass detector identifies caller→callee relationships across service boundaries.

### `ApiBridgeDetector`

#### Pass 1 — `register_endpoints(java_files, fqn_map, geid_map)`
Scans all Java files for Spring mapping annotations:
- `@GetMapping("/path")`, `@PostMapping(...)`, `@PutMapping(...)`, `@DeleteMapping(...)`, `@PatchMapping(...)`
- `@RequestMapping(value="/path", method=RequestMethod.GET)`

Builds an internal endpoint registry: `[{path, http_method, handler_fqn, handler_geid}]`

#### Pass 2 — `detect_calls(java_files, fqn_map, geid_map) → list[RemoteCallEdge]`
Scans all files for HTTP client invocations:
- `restTemplate.getForObject("url", ...)`
- `webClient.get().uri("url", ...)`
- `HttpGet("url")`, `FeignClient.get("url")`

Extracts the URL literal, strips query strings, normalises path variables (`/users/{id}` ↔ `/users/123`), and matches against the registered endpoint registry.

Returns `RemoteCallEdge` objects → loader creates `REMOTE_CALLS` edges.

---

## `graph/schema.py` — Neo4j Schema Setup

**Purpose**: Creates all constraints and indexes on startup. Safe to run every time — all statements use `IF NOT EXISTS`.

### Constraints (8)
Unique constraints on `geid` for: `Project`, `Module`, `Component`, `LogicUnit`  
Unique constraints on `name`/`fqn` for: `AnnotationType`, `ExceptionType`, `EventClass`  
Unique constraints on `name` for: `DatabaseTable` (Phase 2)  
Unique constraints on `config_key` for: `Configuration` (Phase 2)

### Indexes (10)
- `component_fqn` — fast MATCH by class name
- `logicunit_fqn` — fast MATCH by method name
- `logicunit_community` / `component_community` — community-based queries
- `logicunit_return_type` — data flow analysis
- `component_event_handler` — WSO2 event handler queries
- `component_kind` / `logicunit_kind` — type filtering
- `dbtable_repo` — DatabaseTable by repo_name (Phase 2)
- `config_type` — Configuration by config_type (Phase 2)

### Fulltext Index
`code_search` — fulltext search across `fqn` and `docstring` on both `LogicUnit` and `Component` nodes.

---

## `graph/loader.py` — Neo4j Bulk Loader

**Purpose**: Takes UIR objects and creates/updates Neo4j nodes and relationships using `MERGE` (idempotent upsert). All write queries call `.consume()` to prevent lazy-execution silent drops.

### Critical Design: `.consume()` on All Writes
Neo4j's Python driver is **lazy** — `session.run()` only *prepares* the query. Without consuming the result, write queries may never execute. Every write in this file ends with `.consume()`.

### Public Methods

| Method | Creates | Tier |
|---|---|---|
| `load_project(project)` | Project→Module→Component→LogicUnit nodes (skeleton) | Always |
| `load_dependency_edges(mod_geid, deps)` | `DEPENDS_ON` | 1 |
| `load_implements_extends(all_components)` | `IMPLEMENTS` + `EXTENDS` (2-pass bulk) | 1 |
| `load_call_graph(logic_units)` | `CALLS` (APOC batch) | 1 |
| `load_type_edges(logic_units)` | `RETURNS` + `RECEIVES` | 2 |
| `load_injection_edges(all_components)` | `INJECTS` | 2 |
| `load_annotated_with(all_components)` | `ANNOTATED_WITH` | 2 |
| `load_remote_calls(edges)` | `REMOTE_CALLS` | 2 |
| `load_throws_edges(logic_units)` | `THROWS` | 3 |
| `load_overrides_edges(logic_units)` | `OVERRIDES` | 3 |
| `load_instantiates_edges(logic_units)` | `INSTANTIATES` | 3 |
| `load_event_handler_edges(all_components)` | `HANDLES_EVENT` | 3 |
| `load_database_tables(tables)` | `DatabaseTable` nodes | Phase 2 |
| `load_queries_table_edges(edges)` | `QUERIES_TABLE` | Phase 2 |
| `load_configuration_nodes(configs)` | `Configuration` nodes | Phase 2 |
| `load_reads_config_edges(edges)` | `READS_CONFIG` | Phase 2 |

### Why Two-Pass for IMPLEMENTS/EXTENDS?
Both nodes must exist before an edge can be created with `MATCH`. By calling `load_implements_extends()` *after* all `load_project()` calls, all `Component` nodes are guaranteed to exist in the database.

---

## `graph/gds_client.py` — Graph Data Science Client

**Purpose**: Manages the Neo4j GDS in-memory graph projection and runs the Leiden community detection algorithm.

### Class: `GDSClient`

#### `run_leiden() → dict`
Full Leiden workflow:
1. Drops any existing projection with the same name (cleanup)
2. Queries `db.relationshipTypes()` → dynamically builds projection
3. Projects 5 node labels + all present candidate relationship types
4. Runs `gds.leiden.write` → writes `community_id` to each node
5. Drops the projection (frees GDS memory)
6. Returns `{communityCount, modularity, ranLevels}`

#### Dynamic Projection (Key Design)
The projection only includes relationship types that **actually exist** in the database:
```python
existing = db.relationshipTypes()  # e.g. ["CALLS", "IMPLEMENTS", ...]
project = [r for r in CANDIDATES if r in existing]
```
This prevents the `Failed to invoke procedure gds.leiden.write` crash on partial ingests.

#### Other Methods
| Method | Description |
|---|---|
| `get_community_count()` | Count distinct `community_id` values |
| `get_nodes_by_community(cid)` | All `LogicUnit` nodes in a community |
| `list_community_ids()` | All distinct community IDs in the graph |

---

## `graph/tagger.py` — EntryPoint / DataSink Tagger (Phase 2)

**Purpose**: Tags Neo4j nodes with secondary labels for execution flow analysis. `:EntryPoint` marks API endpoints; `:DataSink` marks database access classes.

### Class: `NodeTagger`

#### `tag_all() → dict`
Runs all tagging passes and returns `{entry_points: int, data_sinks: int}`.

| Pass | Labels Applied | Detection Criteria |
|---|---|---|
| `_tag_entry_points()` | `:EntryPoint` | `@RequestMapping`, `@Path`, `HttpServlet`, Servlet/Controller/Endpoint/Resource FQN patterns |
| `_tag_data_sinks()` | `:DataSink` | `@Repository`, DAO/Repository class patterns, `QUERIES_TABLE` edges, SQL execution method call indicators |

---

## `graph/flow_extractor.py` — GDS Dijkstra Flow Extractor (Phase 2)

**Purpose**: Uses GDS Dijkstra Shortest Path to find primary execution paths between EntryPoints and DataSinks.

### Class: `FlowExtractor`

#### `extract_all_flows() → list[FlowPath]`
1. Projects execution-flow edges (CALLS, INJECTS, IMPLEMENTS, OVERRIDES, REMOTE_CALLS) into a GDS graph `nexus-flow-graph`
2. Queries all `:EntryPoint` and `:DataSink` nodes
3. For each (EntryPoint, DataSink) pair: `gds.shortestPath.dijkstra.stream`
4. Enriches each path with `READS_CONFIG` config keys and `QUERIES_TABLE` table names
5. Drops the GDS graph projection
6. Returns `FlowPath` objects

### `FlowPath`
| Field | Description |
|---|---|
| `entry_fqn` | Starting API endpoint FQN |
| `sink_fqn` | Terminal database/repository FQN |
| `path_fqns` | Ordered list of FQNs along the path |
| `config_keys` | Configuration keys read along the path |
| `table_names` | Database tables accessed along the path |
| `total_cost` | GDS Dijkstra path cost |

---

## `pipeline/mirror.py` — Repository Mirror

**Purpose**: Clones or pulls Git repositories defined in `repos.yaml` into the local `./mirror/` directory.

### `RepositoryMirror`

#### `mirror_all(config_path) → list[Path]`
Reads `repos.yaml`, calls `mirror_repo()` for each entry, returns list of local paths.

#### `mirror_repo(repo_config) → Path`
- **First run**: `git clone --branch {branch} {url} mirror/{name}`
- **Subsequent runs**: `git pull origin {branch}` in the existing mirror directory
- Returns the local path for downstream processing

---

## `pipeline/orchestrator.py` — Ingestion Pipeline Orchestrator

**Purpose**: The top-level coordinator that sequences all pipeline stages. This is the entry point called by `main.py`.

### Class: `IngestionPipeline`

The `__init__` method wires up all clients: Neo4j driver, ChromaDB, Redis, and all the worker components.

#### `run(config_path, skip_summarization) → dict`
Executes the complete pipeline in order:

1. `apply_schema()` — idempotent schema setup
2. `mirror.mirror_all()` — Stage 1: clone/pull repos
3. Per-repo loop: parse Java → load Neo4j nodes → embed ChromaDB
4. Cross-repo link phase (Tier 1 → Tier 2 → Tier 3 edges)
5. `gds.run_leiden()` — community detection
6. `summarizer.summarize_all()` — LLM summaries (optional)

Returns stats dict: `{repos_mirrored, files_parsed, components, logic_units, call_edges, implements_edges, depends_on_edges, remote_calls, communities}`

**Status tracking**: Writes current stage name to Redis key `nexus:pipeline:stage` — can be polled by a monitoring dashboard.

---

## `vectorstore/chunker.py` — UIR Chunker

**Purpose**: Converts `LogicUnit` objects into `EmbeddingChunk` objects ready for ChromaDB. Implements **functional chunking** — one method = one chunk — rather than character-count-based splitting.

### `UIRChunker`

#### `chunk_logic_unit(lu) → list[EmbeddingChunk]`
Returns 1–2 chunks per `LogicUnit`:

- **`code_logic`** chunk (always): method signature + raw body text. Captures *what the code does*.
- **`code_intent`** chunk (if Javadoc exists): parsed and formatted Javadoc. Captures *what the developer intended*.

#### `EmbeddingChunk` Fields
| Field | Description |
|---|---|
| `chunk_id` | `"{geid}_{chunk_type}"` — unique ChromaDB document ID |
| `geid` | GEID bridge to Neo4j node |
| `fqn` | Fully qualified name |
| `text` | Text to embed |
| `chunk_type` | `"code_logic"` or `"code_intent"` |

---

## `vectorstore/embedder.py` — ChromaDB Embedder

**Purpose**: Manages two ChromaDB collections and handles batch upsert of `EmbeddingChunk` objects.

### Collections
| Collection | Embeds | Use Case |
|---|---|---|
| `code_logic` | Method body text | "Find code that does X" |
| `code_intent` | Javadoc description text | "Find code intended for X" |

Both use `all-MiniLM-L6-v2` (384-dimensional embeddings, fast inference, multilingual).

### Key Methods
| Method | Description |
|---|---|
| `upsert_chunks(chunks)` | Batch upsert, splits by type, batches at 100 per call |
| `semantic_search(query, collection, n_results)` | KNN similarity search |
| `get_by_geid(geid, collection)` | Direct GEID lookup |

---

## `community/models.py` — Community Summary Model

**Purpose**: Pydantic model for a generated community summary.

### `CommunitySummary`
| Field | Description |
|---|---|
| `community_id` | Leiden integer community ID |
| `summary_text` | LLM-generated architectural summary |
| `node_count` | Number of `LogicUnit` members |
| `fqn_list` | All member FQNs |
| `top_fqns` | Top 5 representative methods |
| `llm_model` | Model used for generation |
| `token_count` | Total prompt tokens consumed |
| `prompt_truncated` | `True` if prompt was budget-capped |
| `generated_at` | UTC timestamp |

---

## `community/prompt_builder.py` — Community Prompt Builder

**Purpose**: Builds token-budget-enforced prompts for community summarization.

### `build_community_prompt(community_id, nodes) → tuple[str, bool]`
Constructs a prompt listing all member FQNs and docstrings. If the total exceeds `CONTEXT_BUDGET` tokens (7,000), it truncates nodes from the end and returns `(prompt, True)` to signal truncation.

### `SYSTEM_PROMPT`
Instructs the LLM to act as a senior Java architect, summarise the community's architectural role, and identify cross-cutting concerns.

---

## `community/summarizer.py` — Community Summarizer

**Purpose**: For each Leiden community, fetches member nodes, calls GPT-4o-mini, and stores the summary in ChromaDB.

### `CommunitySummarizer`

#### `summarize_all() → list[CommunitySummary]`
Iterates all community IDs from `gds_client.list_community_ids()`, calls `summarize_community()` for each, logs errors without stopping the pipeline.

#### `summarize_community(community_id) → CommunitySummary`
1. `gds_client.get_nodes_by_community(cid)` → list of nodes
2. `build_community_prompt(cid, nodes)` → token-capped prompt
3. OpenAI chat completion → summary text
4. `_upsert_to_chroma(summary)` → stores in `community_summaries` collection

---

## `community/global_rollup.py` — Global GraphRAG Rollup (Phase 2)

**Purpose**: Implements the Microsoft GraphRAG hierarchical rollup — generates Level 2 (Sub-System) and Level 3 (Global Architecture) summaries from Level 1 community summaries.

### Class: `GlobalRollup`

#### `run_full_rollup() → dict`
Complete hierarchical rollup:
1. Fetches all L1 community summaries from ChromaDB `community_summaries`
2. Groups them into 11 domains via keyword heuristics
3. Generates L2 sub-system summaries (one per domain) via GPT-4o-mini
4. Generates L3 Global Architecture Document from all L2 summaries
5. Returns `{l2_count, l3_generated, domains}`

#### Domain classification (11 sub-systems)
| Domain | Keyword triggers |
|---|---|
| Authentication | auth, login, password, credential, SSO, SAML |
| OAuth2 | oauth, token, grant, scope, client_id, bearer |
| SCIM | scim, user provisioning, group management |
| Federation | federated, identity provider, IdP, SAML |
| Session | session, cookie, cache, state management |
| Consent | consent, approval, permission management |
| DCR | dynamic client registration, DCR, client management |
| Discovery | discovery, well-known, OpenID configuration |
| CIBA | CIBA, backchannel, polling, push notification |
| Token Exchange | token exchange, impersonation, delegation, act claim |
| Utility / Common | (default bucket for unmatched communities) |

#### `get_global_summary() → str`
Returns the L3 Global Architecture Document.

#### `get_subsystem_summary(domain) → str`
Returns the L2 summary for a specific domain.

---

## `reasoning/pipeline.py` — GraphRAG Reasoning Pipeline

**Purpose**: Orchestrates the complete Map-Reduce reasoning query — the public API for the agent layer.

### `GraphRAGPipeline`

#### `analyze(pr_summary) → str`
1. `MapStep.run(pr_summary)` → scored community list
2. `ReduceStep.run(map_results)` → final review string

Returns a markdown-formatted architectural impact review.

---

## `reasoning/flow_summarizer.py` — Flow Narrative Summariser (Phase 2)

**Purpose**: Generates natural-language "End-to-End Architectural Stories" from GDS Dijkstra execution paths.

### Class: `FlowNarrativeSummarizer`

#### `summarize_all(flow_paths) → list[FlowNarrative]`
Parallel LLM calls via `ThreadPoolExecutor`. For each `FlowPath`:
1. Builds a structured prompt with entry FQN, sink FQN, path nodes, config keys, table names
2. Calls GPT-4o-mini to produce a narrative
3. Upserts to ChromaDB `flow_narratives` collection

### `FlowNarrative`
| Field | Description |
|---|---|
| `flow_id` | `"flow_{entry_hash}_{sink_hash}"` |
| `entry_fqn` | Starting API endpoint |
| `sink_fqn` | Terminal database class |
| `narrative_text` | GPT-4o-mini generated story |
| `path_length` | Number of nodes in the execution path |

---

## `reasoning/map_step.py` — Map Step

**Purpose**: First stage of GraphRAG reasoning. Retrieves community summaries from ChromaDB and scores them for relevance. Operates in two distinct modes.

### `MapStep`

#### `run(input_text, restrict_community_ids, mode) → list[MapResult]`

Dispatches to one of two paths based on `mode`:

| Mode | Trigger | LLM calls | Method called |
|---|---|---|---|
| `question` | All Route C semantic queries | **0** | `_retrieve_candidates_with_distances()` |
| `pr` | Blast-radius PR analysis | Up to 50 (ThreadPoolExecutor) | `_retrieve_candidates()` + `_llm_score()` |

#### Mode: `question` — ChromaDB distance scoring (zero LLM calls)

```python
# Fetches top n_candidates (default 20) with distances included
results = collection.query(query_texts=[question], n_results=n,
                           include=["documents", "metadatas", "distances"])
# Converts ChromaDB cosine distance (0=identical, 2=opposite) to 0-100 score
score = max(0, round((1.0 - distance / 2.0) * 100))
# Typical scores for broad queries: 40-57 (NOT 70-100 — do NOT apply LLM threshold)
```

#### Mode: `pr` — LLM scoring (blast-radius analysis)

```python
# Scaling formula capped at 50 (was 200 before the fix)
n = min(max(n_candidates, total // 4), 50)
# ThreadPoolExecutor calls _score_community() per candidate
```

#### `restrict_community_ids` path

When provided (deterministic routes A/B), bypasses ChromaDB entirely and uses `_retrieve_by_ids()`.  Always goes through `_llm_score()` regardless of `mode` — deterministic IDs have no distance scores.

### `MapResult` Fields
`community_id`, `score` (0–100), `reason` (1 sentence or `"ChromaDB similarity score"`), `summary_text`

### Score interpretation

| Score range | Mode | Meaning |
|---|---|---|
| 70–100 | `pr` LLM | High blast-radius impact |
| 30–69 | `pr` LLM | Moderate impact |
| 0–29 | `pr` LLM | Low / no impact |
| 50–57 | `question` ChromaDB | Top vector similarity hit |
| 40–49 | `question` ChromaDB | Relevant but not a close match |

> **Important**: ChromaDB distance scores never reach 70. Applying the LLM threshold (70) to semantic-route map results produces `communities=0` and an empty response. The main pipeline passes all map results through; only `_build_summaries_text()` applies a `>= 50` filter internally.

---

## `reasoning/reduce_step.py` — Reduce Step

**Purpose**: Synthesises the final answer from Map results and grounded evidence. Uses query-intent detection to choose the correct system prompt, template, and token budget.

### `ReduceStep`

#### `run(map_results, query, primary_targets, code_snippets) → str`

1. `_detect_intent(query)` — classifies into one of 6 intents
2. `_build_summaries_text(map_results, budget)` — assembles community block text, trims from lowest-score end if over token budget, **always keeps at least 1 entry**
3. `_format_code_snippets(snippets, budget)` — formats code blocks, respects per-intent budget
4. Selects system prompt + template based on intent
5. Single LLM call with `max_completion_tokens` set per intent

### Intent Detection — `detect_query_intent(query) → str`

Module-level function importable by `main.py` without instantiating `ReduceStep`.

| Intent | Regex / Keywords | Output Tokens | System Prompt |
|---|---|---|---|
| `code` | `show me`, `give me`, `how is X implemented`, `source of` | 1 800 | `SYSTEM_IMPACT` |
| `safety` | `can I remove`, `is it safe`, `dead code`, `safe to delete` | 1 800 | `SYSTEM_SAFETY` |
| `capability` | `does this support`, `is X enforced`, `can X bypass` | 1 800 | `SYSTEM_CAPABILITY` |
| `impact` | `blast radius`, `what breaks`, `impact`, `dependency` | 1 800 | `SYSTEM_IMPACT` |
| `narrative` | `full story`, `end-to-end`, `how does X get evaluated`, `walk me through`, `explain how`, `step by step` | **6 000** | `SYSTEM_NARRATIVE` |
| `general` | *(fallback)* | **6 000** | `SYSTEM_GENERAL` |

### `_build_summaries_text(map_results, budget)`

- Filters `score >= 50`; if none qualify, falls back to top-10 by score
- Trims from the bottom of the list until within `budget` tokens
- **Always retains at least 1 entry** (was a bug: the previous `while lines:` loop could empty the list when a single summary exceeded the budget)
- For narrative queries, `NARRATIVE_SUMMARY_BUDGET` (~12 900 tokens) is passed instead of the default `SUMMARY_BUDGET` (~12 000)

### Token Budget Constants

| Constant | Value | Used for |
|---|---|---|
| `OUTPUT_RESERVE` | 1 800 | safety / capability / impact / code |
| `NARRATIVE_RESERVE` | 6 000 | narrative / general |
| `SNIPPET_BUDGET` | 50% of DATA_BUDGET | default code snippet budget |
| `SUMMARY_BUDGET` | 40% of DATA_BUDGET | default community summary budget |
| `NARRATIVE_SNIPPET_BUDGET` | 40% of NARRATIVE_DATA_BUDGET | narrative code snippets |
| `NARRATIVE_SUMMARY_BUDGET` | 50% of NARRATIVE_DATA_BUDGET | narrative community summaries |

---

## `sample_repos/repos.yaml` — Repository Configuration

**Purpose**: Declares which Git repositories to mirror and ingest.

```yaml
repos:
  - name: identity-oauth2-grant-token-exchange
    url: https://github.com/wso2-extensions/identity-oauth2-grant-token-exchange
    branch: main
  - name: identity-inbound-auth-oauth
    url: https://github.com/wso2-extensions/identity-inbound-auth-oauth
    branch: master
```

Each entry: `name` (used in GEID generation), `url` (git remote), `branch` (default branch).

---

## `docker-compose.yml` — Services

**Purpose**: Starts the three required backing services.

| Service | Image | Port | Purpose |
|---|---|---|---|
| `neo4j` | `neo4j:5.x` with APOC + GDS plugins | 7474 (HTTP), 7687 (Bolt) | Property graph + Leiden + Dijkstra |
| `chromadb` | `ghcr.io/chroma-core/chroma:0.4.15` | 8000 | Vector store (6 collections) |
| `redis` | `redis:7` | 6379 | Pipeline state |

```bash
docker compose up -d      # start services
docker compose down -v    # stop + remove volumes (full reset)
```
