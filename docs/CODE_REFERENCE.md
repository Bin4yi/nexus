# CodeNexus — Code Reference

Per-file documentation for all source modules. Organized bottom-up from data models to entry point.

---

## `main.py` — CLI Entry Point

**Purpose**: Single entry point for all CodeNexus operations.

### Commands
```bash
py main.py ingest         # Run the full ingestion pipeline
py main.py query "..."    # Ask a question (three-tier router)
py main.py explain <name> # Explain a method or class
py main.py trace <name>   # Show call chain (no LLM)
py main.py callers <name> # List all callers
py main.py find <text>    # Exact text search (no LLM)
py main.py debug <error>  # Root-cause analysis
```

### Functions
| Function | Description |
|---|---|
| `ingest()` | Instantiates `IngestionPipeline`, calls `.run()`, logs stats. Exits with code 1 if any repos failed. |
| `main()` | `argparse` router — parses subcommand, dispatches to appropriate function. |

---

## `config/settings.py` — Central Configuration

**Purpose**: Loads all environment variables from `.env` into a typed `Settings` Pydantic model. All modules import the singleton `settings` from here.

### Key Settings

| Setting | Env Var | Default | Description |
|---|---|---|---|
| `neo4j_uri` | `NEO4J_URI` | `bolt://localhost:7687` | Neo4j connection |
| `neo4j_user` | `NEO4J_USER` | `neo4j` | Auth username |
| `neo4j_password` | `NEO4J_PASSWORD` | `nexuspassword` | Auth password |
| `chroma_host` | `CHROMA_HOST` | `localhost` | ChromaDB host |
| `chroma_port` | `CHROMA_PORT` | `8000` | ChromaDB port |
| `redis_url` | `REDIS_URL` | `redis://localhost:6379/0` | Redis connection |
| `llm_provider` | `LLM_PROVIDER` | `openai` | `openai` or `azure` |
| `llm_api_key` | `LLM_API_KEY` | `""` | API key |
| `llm_fast_model` | `LLM_FAST_MODEL` | `gpt-4o-mini` | Bulk ops model |
| `llm_strong_model` | `LLM_STRONG_MODEL` | `gpt-4o` | Final answer model |
| `llm_azure_endpoint` | `LLM_AZURE_ENDPOINT` | `""` | Azure endpoint URL |
| `llm_azure_api_version` | `LLM_AZURE_API_VERSION` | `2025-04-01-preview` | Azure API version |
| `llm_deployment` | `LLM_DEPLOYMENT` | `""` | Azure deployment name |
| `max_context_tokens` | `MAX_CONTEXT_TOKENS` | `8000` | Token budget ceiling |
| `gds_graph_name` | `GDS_GRAPH_NAME` | `codenexus-graph` | GDS in-memory graph name |
| `leiden_gamma` | `LEIDEN_GAMMA` | `1.5` | Leiden resolution (higher = smaller communities) |
| `gds_leiden_relationships` | `GDS_LEIDEN_RELATIONSHIPS` | `CALLS,INJECTS,...` | Edge types for Leiden projection |
| `grep_backend` | `GREP_BACKEND` | `ripgrep` | Lexical search backend |
| `community_summarization_enabled` | `COMMUNITY_SUMMARIZATION_ENABLED` | `true` | Toggle LLM summarization |
| `embedding_model` | `EMBEDDING_MODEL` | `all-MiniLM-L6-v2` | HuggingFace embedding model |
| `summarizer_max_workers` | `SUMMARIZER_MAX_WORKERS` | `4` | Concurrent LLM calls |
| `batch_size` | `BATCH_SIZE` | `500` | Neo4j APOC batch size |
| `retry_max_attempts` | `RETRY_MAX_ATTEMPTS` | `3` | Tenacity retry attempts |
| `log_level` | `LOG_LEVEL` | `INFO` | Root log level |

### `make_llm_client(tier="fast")` — LLM Factory Method

All modules call `settings.make_llm_client(tier)` instead of constructing `OpenAI()` directly.
`tier` accepts `"fast"` (gpt-4o-mini for bulk ops) or `"strong"` (gpt-4o for final answers).
For Azure, the deployment name controls the model — `tier` is ignored.

```python
def make_llm_client(self, tier: str = "fast"):
    if self.llm_provider.lower() == "azure":
        from openai import AzureOpenAI
        return AzureOpenAI(
            api_key=self.llm_api_key,
            azure_endpoint=self.llm_azure_endpoint,
            api_version=self.llm_azure_api_version,
        )
    from openai import OpenAI
    return OpenAI(api_key=self.llm_api_key)

def get_model_name(self, tier: str = "fast") -> str:
    return self.llm_strong_model if tier == "strong" else self.llm_fast_model
```

### Two-Tier Cost Strategy

| Tier | Model | Where used |
|---|---|---|
| `fast` | `gpt-4o-mini` | Community summarisation (bulk), map-step scoring, query expansion, L2 rollup |
| `strong` | `gpt-4o` | Final reduce answer, L3 global rollup only |

This minimises API cost: ~90% of LLM calls use the cheap model.
A full ingest of 100 repos costs ~$0.50 vs ~$5+ if all operations used gpt-4o.

---

## `parsers/uir.py` — Universal Intermediate Representation

**Purpose**: Pydantic models defining the language-agnostic data contract between parsers and all downstream components.

### `Parameter`

A single method/constructor parameter.

| Field | Type | Description |
|---|---|---|
| `name` | `str` | Parameter identifier |
| `type_name` | `str` | Java type (generic types preserved, e.g. `List<User>`) |
| `doc` | `str` | Javadoc `@param` description |
| `annotations` | `list[dict]` | Annotations on the parameter, e.g. `[{"name": "QueryParam", "value": "client_id"}]` |

### `FieldDeclaration`

A field declared in a class body.

| Field | Type | Description |
|---|---|---|
| `name` | `str` | Field identifier |
| `type_name` | `str` | Declared Java type |
| `annotations` | `list[str]` | Raw annotation strings (used for DI detection) |
| `is_injected` | `bool` | `True` if annotated with `@Autowired`, `@Inject`, `@Resource`, or `@Reference` |

### `LogicUnit`

The atomic semantic unit — a single Java method or constructor. Maps to a Neo4j `:LogicUnit` node and two ChromaDB vectors.

| Field | Type | Description |
|---|---|---|
| `geid` | `str` | SHA256(repo::fqn)[:16] — cross-DB bridge key |
| `fqn` | `str` | Fully qualified name: `com.example.Service.getUser(String)` |
| `kind` | `str` | `"method"` or `"constructor"` |
| `parameters` | `list[Parameter]` | Method parameters (with type, doc, and annotations) |
| `return_type` | `str` | Java return type string |
| `body_text` | `str` | Raw source body for embedding |
| `docstring` | `str` | Javadoc description block |
| `annotations` | `list[dict]` | Method-level annotations as structured dicts, e.g. `[{"name": "Override"}, {"name": "QueryParam", "value": "code"}]` |
| `calls` | `list[str]` | FQNs of directly called methods → `CALLS` edges |
| `throws` | `list[str]` | Exception type names from `throws` clause → `THROWS` edges |
| `overrides` | `str \| None` | Parent method FQN if `@Override` detected → `OVERRIDES` edge |
| `instantiates` | `list[str]` | Class names from `new X()` → `INSTANTIATES` edges |
| `visibility` | `str` | `"public"` \| `"protected"` \| `"private"` \| `"package"` |
| `is_static` | `bool` | Whether the method is `static` |
| `is_abstract` | `bool` | Whether the method is `abstract` |
| `is_final` | `bool` | Whether the method is `final` |
| `is_synchronized` | `bool` | Whether the method is `synchronized` |
| `lifecycle_role` | `str \| None` | OSGi lifecycle: `"activate"` \| `"deactivate"` \| `"modified"` \| `None` |

### `Component`

A Java class, interface, enum, or annotation type. Maps to a Neo4j `:Component` node.

| Field | Type | Description |
|---|---|---|
| `geid` | `str` | Cross-DB bridge key |
| `fqn` | `str` | `com.example.auth.UserService` |
| `kind` | `str` | `"class"`, `"interface"`, `"enum"`, `"annotation"` |
| `implements` | `list[str]` | Interface FQNs → `IMPLEMENTS` edges |
| `extends` | `str \| None` | Parent class FQN → `EXTENDS` edge |
| `logic_units` | `list[LogicUnit]` | Nested method objects |
| `fields` | `list[FieldDeclaration]` | Field objects → `HAS_FIELD` + `INJECTS` edges |
| `annotations` | `list[dict]` | Class-level annotations as structured dicts |
| `is_event_handler` | `bool` | `True` if extends `AbstractEventHandler` or implements `EventHandler` |
| `visibility` | `str` | `"public"` \| `"protected"` \| `"private"` \| `"package"` |
| `is_abstract` | `bool` | Whether the class is `abstract` |
| `is_final` | `bool` | Whether the class is `final` |

### `Module`

A Maven artifact (one `pom.xml`). Contains `components` and `dependencies` (→ `DEPENDS_ON` edges).

### `Project`

A Git repository. Contains `modules`. Top of the containment hierarchy.

---

## `parsers/java_parser.py` — Java AST Parser

**Purpose**: Parses Java source files into UIR objects using Tree-sitter. All structural and semantic knowledge extraction happens here.

### Class: `JavaParser`

#### `parse_file(file_path, repo_name) → list[Component]`

Main entry point. Returns all `Component` objects with their `LogicUnit` children.

**Algorithm:**
1. Parse bytes → Tree-sitter syntax tree
2. Extract package declaration and import statements
3. Walk `class_declaration`, `interface_declaration`, `enum_declaration` nodes
4. For each type: extract modifiers, superclass, interfaces, body

#### `_parse_annotation(node, source) → dict`

Converts a Tree-sitter `marker_annotation` or `annotation` node into a structured dict.

- `marker_annotation` (no args): `{"name": "Override"}`
- Single-value `annotation` (`@Value("${key}")`): `{"name": "Value", "value": "${key}"}`
- Named-attribute `annotation` (`@Reference(cardinality=MANDATORY)`): `{"name": "Reference", "cardinality": "MANDATORY"}`

This enables: OSGi `@Reference` cardinality, JAX-RS `@QueryParam` name extraction, Spring `@Value` config key parsing.

#### What Each Extraction Method Does

| Method | Extracts | Graph Edge |
|---|---|---|
| `_extract_package` | Package name string | (context) |
| `_extract_imports` | `{simple_name: fqn}` map for type resolution | (context) |
| `_extract_extends` | Parent class FQN | `EXTENDS` |
| `_extract_implements` | All implemented interface FQNs | `IMPLEMENTS` |
| `_extract_parameters` | Parameters with types **and parameter-level annotations** | `RECEIVES` signal |
| `_extract_return_type` | Return type string (generic types preserved) | `RETURNS` signal |
| `_extract_calls` | All `method_invocation` nodes + **lambda body recursion** | `CALLS` |
| `_extract_throws` | Exception types from `throws` clause | `THROWS` |
| `_extract_instantiations` | Class names from `object_creation_expression` | `INSTANTIATES` |
| `_extract_fields` | Field declarations; sets `is_injected=True` if DI annotation present | `HAS_FIELD`, `INJECTS` |
| `_extract_annotations` | Structured annotation dicts via `_parse_annotation()` | `ANNOTATED_WITH` |
| `_is_event_handler` | Checks `extends`/`implements` against WSO2 event base types | `HANDLES_EVENT` |

#### Modifier Extraction

In `_parse_method_or_constructor`, extracts the `modifiers` child node and sets:

```python
modifiers_node = node.child_by_field_name("modifiers")
if modifiers_node:
    mod_text = source[modifiers_node.start_byte:modifiers_node.end_byte].decode("utf-8")
    lu.visibility = next((m for m in ["public","protected","private"] if m in mod_text), "package")
    lu.is_static = "static" in mod_text
    lu.is_abstract = "abstract" in mod_text
    lu.is_final = "final" in mod_text
    lu.is_synchronized = "synchronized" in mod_text
```

#### OSGi Lifecycle Detection

After annotation extraction, the parser checks for OSGi lifecycle annotations:

```python
_OSGI_LIFECYCLE = {"Activate": "activate", "Deactivate": "deactivate", "Modified": "modified"}
for ann in annotations:
    if ann.get("name", "") in _OSGI_LIFECYCLE:
        lifecycle_role = _OSGI_LIFECYCLE[ann["name"]]
```

#### Generic Type Preservation

Generic types are now preserved in `type_name` fields (e.g. `List<User>`, `Map<String,Object>`).
Generics are stripped only when constructing FQN-based graph MERGE keys via `_type_for_graph_key()`.

#### `@Override` Detection

Updated to work with the dict-format annotations:
```python
if any(a.get("name") == "Override" for a in annotations) and parent_extends:
    lu.overrides = f"{parent_extends}.{method_name}"
```

#### Lambda / Stream Call Extraction

The `_walk_calls` method now recurses into lambda expression bodies, capturing calls inside
`.stream().filter(x -> x.method())` chains that were previously invisible.

---

## `parsers/sql_schema_parser.py` — SQL DDL Parser

**Purpose**: Scans `dbscripts/` folders for `CREATE TABLE` DDL statements. Creates `DatabaseTable` nodes in Neo4j and links DAO classes via `QUERIES_TABLE` edges.

### Class: `SQLSchemaParser`

#### `scan_repo(repo_path, repo_name) → tuple[list[DatabaseTableInfo], list[TableQueryEdge]]`

Returns:
- `DatabaseTableInfo`: `{name, repo_name, file_path, columns}` → `DatabaseTable` nodes
- `TableQueryEdge`: `{component_fqn, table_name}` → `QUERIES_TABLE` edges

Detection heuristics:
- **Table extraction**: Regex `CREATE TABLE [IF NOT EXISTS] [schema.]table_name`
- **DAO detection**: Class names matching DAO/Repository patterns + SQL execution method calls

---

## `parsers/config_parser.py` — Configuration File Parser

**Purpose**: Parses WSO2 `deployment.toml`, `repository/conf/*.xml`, `*.properties`, and `application.yml` files. Creates `Configuration` nodes and `READS_CONFIG` edges.

### Class: `ConfigurationParser`

#### `scan_config_files(repo_path, repo_name) → list[ConfigurationInfo]`

Discovers and parses configuration files:
1. `deployment.toml` — via `tomllib` (Python 3.11+ stdlib) with regex fallback
2. `repository/conf/*.xml`, `conf/*.xml`, `src/main/resources/*.xml` — XML element names + `${placeholder}` extraction
3. `*.properties` files — `key=value` and `key: value` patterns
4. `application.yml` / `application.yaml` — top-level YAML keys

Returns `ConfigurationInfo` objects: `{config_key, config_type, source_file, repo_name}`

#### `detect_config_readers(java_files, components, known_config_keys) → list[ConfigReadEdge]`

Scans Java source files for config-reading patterns:
- `IdentityUtil.getProperty(...)`, `@Value("${key}")`, `Environment.getProperty(...)`
- `CarbonUtils.getServerConfiguration()`, `@ConfigurationProperties`
- `OAuthServerConfiguration`, `IdentityConfigParser`, `FileBasedConfigurationBuilder`

Returns `ConfigReadEdge` objects: `{component_geid, component_fqn, config_key}`

#### Supported file formats and key extraction

| Format | Key extraction strategy |
|---|---|
| `deployment.toml` | `tomllib.load()` → recursive flatten to dotted keys (e.g. `[server.oauth2]` → `server.oauth2`) |
| `*.xml` | Element names (non-generic) + `${property.name}` placeholders |
| `*.properties` | `key=value` and `key: value` line patterns |
| `*.yml` / `*.yaml` | Top-level YAML keys at indentation=0 |

#### `tomllib` Integration (Python 3.11+)

The TOML parser now uses `tomllib` from the Python 3.11+ standard library for accurate
nested table handling. This correctly resolves `[[array.of.tables]]` and `[nested.section]`
hierarchies. Falls back to regex-based parsing for Python < 3.11 or malformed TOML files.

```python
if sys.version_info >= (3, 11):
    import tomllib
    with open(toml_file, "rb") as f:
        data = tomllib.load(f)
    self._flatten_toml_dict(data, "", toml_file, repo_name, seen, configs)
```

---

## `parsers/geid.py` — GEID Generator

**Purpose**: Generates the **Global Entity Identifier** — the 16-character hex key shared between Neo4j and ChromaDB.

```python
geid = SHA256(f"{repo_name}::{fqn}").hexdigest()[:16]
```

Properties: deterministic, unique per entity, repository-scoped, fixed length.

---

## `parsers/fqn_builder.py` — FQN Construction

| Function | Output |
|---|---|
| `build_component_fqn(package, name)` | `com.example.UserService` |
| `build_logic_unit_fqn(pkg, cls, method, param_types)` | `com.example.UserService.getUser(String,int)` |
| `build_call_fqn(receiver, method, resolved_fqn)` | Best-effort FQN for a call site |

---

## `parsers/javadoc_parser.py` — Javadoc Parser

Extracts structured data from `/** ... */` comment blocks.

### `JavadocParser.parse(raw_comment) → dict`
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
Converts parsed dict to flat text optimised for semantic embedding (no `@tag` noise).

---

## `linker/maven_resolver.py` — Maven Dependency Resolver

**Purpose**: Reads `pom.xml` files to extract module identity and Maven dependency declarations, which become `DEPENDS_ON` edges.

### `MavenResolver`

| Method | Description |
|---|---|
| `find_poms(repo_path)` | Recursively finds all `pom.xml`, sorted shallow-first |
| `parse_pom(pom_path)` | Returns module identity dict + list of `DependencyEdge` objects |

---

## `linker/api_bridge.py` — REST API Bridge Detector

**Purpose**: Bridges `CALLS` gap between microservices. Detects REST API connections across service boundaries.

### `ApiBridgeDetector`

#### Pass 1 — `register_endpoints(java_files, fqn_map, geid_map)`

Scans for Spring and JAX-RS endpoint annotations:
- Spring: `@GetMapping`, `@PostMapping`, `@PutMapping`, `@DeleteMapping`, `@PatchMapping`, `@RequestMapping`
- JAX-RS: `@GET`, `@POST`, `@PUT`, `@DELETE`, `@PATCH`, `@Path`

**JAX-RS path composition (v2):** Class-level `@Path` is extracted from the class header (before the first `{`) and merged with each method-level `@Path`:

```python
class_base_path = ""
first_brace = source.find("{")
class_header = source[:first_brace] if first_brace != -1 else source
class_base_match = self._JAXRS_PATH_RE.search(class_header)
if class_base_match:
    class_base_path = class_base_match.group(1).strip()

effective_path = self._normalize_path(class_base_path + "/" + method_path)
```

**Media type extraction (v2):** `@Consumes` and `@Produces` annotations are extracted and stored:
```python
_CONSUMES_RE = re.compile(r'@Consumes\s*\(\s*["\']?([^"\')\s]+)["\']?\s*\)')
_PRODUCES_RE = re.compile(r'@Produces\s*\(\s*["\']?([^"\')\s]+)["\']?\s*\)')
```

#### `EndpointRegistration` dataclass

| Field | Description |
|---|---|
| `path` | Effective path (class base + method path, normalised) |
| `http_method` | HTTP verb |
| `handler_fqn` | FQN of the handler method |
| `handler_geid` | GEID of the handler |
| `consumes` | MIME type from `@Consumes` (e.g. `application/json`) |
| `produces` | MIME type from `@Produces` |

#### Pass 2 — `detect_calls(java_files, fqn_map, geid_map) → list[RemoteCallEdge]`

Scans for HTTP client invocations:
- `restTemplate.getForObject("url", ...)`, `webClient.get().uri("url", ...)`
- `HttpGet("url")`, `FeignClient.get("url")`

Extracts URL literal, strips query strings, normalises path variables, matches against endpoint registry.

---

## `graph/schema.py` — Neo4j Schema Setup

**Purpose**: Creates all constraints and indexes on startup. Safe to run every time — all use `IF NOT EXISTS`.

### Uniqueness Constraints

| Node Type | Unique Property |
|---|---|
| `Project` | `geid` |
| `Module` | `geid` |
| `Component` | `geid` |
| `LogicUnit` | `geid` |
| `AnnotationType` | `name` |
| `ExceptionType` | `fqn` |
| `EventClass` | `fqn` |
| `DatabaseTable` | `name` |
| `Configuration` | `config_key` |
| `Specification` | `spec_id` (Sprint 2 prep) |

### Lookup Indexes

| Index | Purpose |
|---|---|
| `component_fqn` | Fast MATCH by class name |
| `logicunit_fqn` | Fast MATCH by method name |
| `logicunit_community` / `component_community` | Community-based queries |
| `logicunit_return_type` | Data flow analysis |
| `component_event_handler` | WSO2 event handler queries |
| `component_kind` / `logicunit_kind` | Type filtering |
| `dbtable_repo` | `DatabaseTable` by repo_name |
| `config_type` | `Configuration` by config_type |
| `logicunit_visibility` | Security analysis: filter by `public`/`private` |
| `component_visibility` | Security analysis: filter by `public`/`private` |
| `logicunit_lifecycle` | OSGi lifecycle queries |
| `spec_rfc` | `Specification` nodes by RFC number (Sprint 2) |

### Fulltext Index

`code_search` — fulltext across `fqn` and `docstring` on both `LogicUnit` and `Component`.

---

## `graph/loader.py` — Neo4j Bulk Loader

**Purpose**: Takes UIR objects and creates/updates Neo4j nodes and relationships using `MERGE` (idempotent). All writes call `.consume()` to prevent lazy-execution silent drops.

### Critical Design: `.consume()` on All Writes

Neo4j's Python driver is **lazy** — `session.run()` only *prepares* the query. Without consuming the result, write queries may never execute. Every write ends with `.consume()`.

### Annotation Storage

`list[dict]` annotations cannot be stored natively in Neo4j (no maps-in-lists). They are serialized as JSON strings before writing and must be deserialized on read:

```python
import json
# Writing
annotations=json.dumps(comp.annotations)   # e.g. '[{"name": "Component"}, {"name": "Transactional"}]'
# Reading (in queries)
# apoc.convert.fromJsonList(n.annotations)
```

### Public Methods

| Method | Creates | Notes |
|---|---|---|
| `load_project(project)` | Project→Module→Component→LogicUnit nodes | Includes visibility, modifiers, lifecycle_role, annotations (JSON) |
| `load_dependency_edges(mod_geid, deps)` | `DEPENDS_ON` | |
| `load_implements_extends(all_components)` | `IMPLEMENTS` + `EXTENDS` (2-pass bulk) | Both nodes must exist first |
| `load_call_graph(logic_units)` | `CALLS` (APOC batch) | |
| `load_type_edges(logic_units)` | `RETURNS` + `RECEIVES` | |
| `load_injection_edges(all_components)` | `INJECTS` | |
| `load_annotated_with(all_components)` | `ANNOTATED_WITH` | Handles both dict and string annotation formats |
| `load_remote_calls(edges)` | `REMOTE_CALLS` | |
| `load_throws_edges(logic_units)` | `THROWS` | |
| `load_overrides_edges(logic_units)` | `OVERRIDES` | |
| `load_instantiates_edges(logic_units)` | `INSTANTIATES` | |
| `load_event_handler_edges(all_components)` | `HANDLES_EVENT` | |
| `load_database_tables(tables)` | `DatabaseTable` nodes | |
| `load_queries_table_edges(edges)` | `QUERIES_TABLE` | |
| `load_configuration_nodes(configs)` | `Configuration` nodes | |
| `load_reads_config_edges(edges)` | `READS_CONFIG` | |

### `load_annotated_with` (Updated)

Now handles both the new `list[dict]` format and legacy `list[str]` format:
```python
ann_name = ann.get("name", "") if isinstance(ann, dict) else ann.lstrip("@").split("(")[0].strip()
```

### New Node Properties Written

**LogicUnit** (added in v2 Sprint 1):
- `visibility` (`"public"` | `"protected"` | `"private"` | `"package"`)
- `is_static`, `is_abstract`, `is_final`, `is_synchronized` (bool)
- `lifecycle_role` (`""` | `"activate"` | `"deactivate"` | `"modified"`)

**Component** (added in v2 Sprint 1):
- `visibility`, `is_abstract`, `is_final`

---

## `graph/gds_client.py` — Graph Data Science Client

### Class: `GDSClient`

#### `run_leiden() → dict`

1. Drop any existing projection with the same name
2. Query `db.relationshipTypes()` → dynamically build projection (resilient to partial ingests)
3. Project 5 node labels + all present candidate relationship types
4. Run `gds.leiden.write` → writes `community_id` to each node
5. Drop the projection (frees GDS memory)
6. Return `{communityCount, modularity, ranLevels}`

#### Dynamic Projection

Only includes relationship types that **actually exist** in the database. Prevents the
`Failed to invoke procedure gds.leiden.write` crash on partial ingests.

The `GDS_LEIDEN_RELATIONSHIPS` setting controls which edge types are projected. Excludes
high-fan-out utility edges (`THROWS`, `RETURNS`, `RECEIVES`, `INSTANTIATES`) that cause "God Node" collapse.

| Method | Description |
|---|---|
| `get_community_count()` | Count distinct `community_id` values |
| `get_nodes_by_community(cid)` | All `LogicUnit` + `Component` nodes in a community |
| `get_community_boundary_edges(cid)` | Inter-community edges (for prompt builder) |
| `list_community_ids()` | All distinct community IDs |

---

## `graph/tagger.py` — EntryPoint / DataSink Tagger

### Class: `NodeTagger`

| Pass | Labels Applied | Detection Criteria |
|---|---|---|
| `_tag_entry_points()` | `:EntryPoint` | `@RequestMapping`, `@Path`, `HttpServlet`, Servlet/Controller/Endpoint/Resource FQN patterns |
| `_tag_data_sinks()` | `:DataSink` | `@Repository`, DAO/Repository patterns, `QUERIES_TABLE` edges, SQL execution method indicators |

---

## `graph/flow_extractor.py` — GDS Dijkstra Flow Extractor

### Class: `FlowExtractor`

#### `extract_all_flows() → list[FlowPath]`

1. Project execution-flow edges into GDS `nexus-flow-graph`
2. Query all `:EntryPoint` and `:DataSink` nodes
3. For each pair: `gds.shortestPath.dijkstra.stream` — O(E log V) per path
4. Enrich paths with `READS_CONFIG` keys and `QUERIES_TABLE` table names
5. Drop the GDS projection

### `FlowPath`

| Field | Description |
|---|---|
| `entry_fqn` | Starting API endpoint FQN |
| `sink_fqn` | Terminal database/repository FQN |
| `path_fqns` | Ordered list of FQNs along the path |
| `config_keys` | Config keys read along the path |
| `table_names` | Database tables accessed along the path |
| `total_cost` | GDS Dijkstra path cost |

---

## `pipeline/mirror.py` — Repository Mirror

### `RepositoryMirror`

| Method | Description |
|---|---|
| `mirror_all(config_path)` | Reads `repos.yaml`, calls `mirror_repo()` for each, returns local paths |
| `mirror_repo(repo_config)` | First run: `git clone`; subsequent: `git pull` |

---

## `pipeline/orchestrator.py` — Ingestion Pipeline Orchestrator

### Class: `IngestionPipeline`

#### `run(config_path, skip_summarization) → dict`

Complete pipeline in order:
1. `apply_schema()` — idempotent schema setup
2. `mirror.mirror_all()` — Stage 1: clone/pull repos
3. Per-repo loop: parse Java → load Neo4j nodes → embed ChromaDB
4. Cross-repo link phase (Tier 1 → Tier 2 → Tier 3 edges)
5. `gds.run_leiden()` — community detection
6. `summarizer.summarize_all()` — LLM summaries (optional, uses fast model)

Returns stats dict: `{repos_mirrored, files_parsed, components, logic_units, call_edges, ...}`.

**Status tracking**: Writes current stage to Redis key `nexus:pipeline:stage`.

---

## `pipeline/incremental.py` — Incremental Updater

**Purpose**: Re-parses only changed files after a PR merge. Updates only affected Neo4j nodes and ChromaDB vectors. Much faster than a full re-ingest.

---

## `pipeline/cli.py` — Click CLI

Defines `nexus ingest`, `nexus update`, `nexus validate`, `nexus stats`, `nexus analyze-pr` commands.

---

## `vectorstore/chunker.py` — UIR Chunker

### `UIRChunker`

#### `chunk_logic_unit(lu) → list[EmbeddingChunk]`

Returns 1–2 chunks per `LogicUnit`:
- **`code_logic`**: method signature + raw body — captures *what the code does*
- **`code_intent`**: parsed and formatted Javadoc — captures *what the developer intended*

### `EmbeddingChunk`

| Field | Description |
|---|---|
| `chunk_id` | `"{geid}_{chunk_type}"` — unique ChromaDB document ID |
| `geid` | GEID bridge to Neo4j node |
| `fqn` | Fully qualified name |
| `text` | Text to embed |
| `chunk_type` | `"code_logic"` or `"code_intent"` |

---

## `vectorstore/embedder.py` — ChromaDB Embedder

### Collections

| Collection | Embeds | Use Case |
|---|---|---|
| `code_logic` | Method body text | "Find code that does X" |
| `code_intent` | Javadoc description text | "Find code intended for X" |

Both use `all-MiniLM-L6-v2` (384-dimensional, CPU-only, no API key).

| Method | Description |
|---|---|
| `upsert_chunks(chunks)` | Batch upsert, splits by type, batches at `EMBEDDING_BATCH_SIZE` per call |
| `semantic_search(query, collection, n_results)` | KNN similarity search |
| `get_by_geid(geid, collection)` | Direct GEID lookup |

---

## `community/models.py` — Community Summary Model

### `CommunitySummary`

| Field | Description |
|---|---|
| `community_id` | Leiden integer community ID |
| `summary_text` | LLM-generated architectural summary |
| `node_count` | Number of member nodes |
| `fqn_list` | All member FQNs |
| `top_fqns` | Top 5 representative methods |
| `llm_model` | Model used (fast model: `gpt-4o-mini`) |
| `token_count` | Total prompt tokens consumed |
| `prompt_truncated` | `True` if prompt was budget-capped |
| `generated_at` | UTC timestamp |

---

## `community/prompt_builder.py` — Community Prompt Builder

**Purpose**: Builds token-budget-enforced prompts for community summarization.

### Token Budget

```python
SYSTEM_TOKENS = 200       # reserved for the system prompt
OUTPUT_RESERVE = 1000     # reserved for the model's output
DATA_BUDGET = settings.max_context_tokens - SYSTEM_TOKENS - OUTPUT_RESERVE
# With max_context_tokens=8000: DATA_BUDGET = 6800 tokens
```

### `build_community_prompt(community_id, nodes, boundary_edges) → tuple[str, bool]`

Constructs a prompt listing all class and method FQNs with docstrings, followed by
inter-community boundary edge summary (optional). If the body exceeds `DATA_BUDGET` tokens,
truncates nodes from the end (lowest priority = last methods) and returns `(prompt, True)`.

The while loop checks `count_tokens(body) <= DATA_BUDGET` — `body` is just the prompt body,
not including the system prompt overhead. This was a bug in v1 that allowed the body to
slightly exceed DATA_BUDGET; fixed in v2.

---

## `community/summarizer.py` — Community Summarizer

### `CommunitySummarizer`

#### `summarize_all() → list[CommunitySummary]`

Iterates all community IDs, calls `summarize_community()` for each.
Uses `ThreadPoolExecutor` with `SUMMARIZER_MAX_WORKERS` concurrent LLM calls.
Logs errors without stopping the pipeline.

#### `summarize_community(community_id) → CommunitySummary`

1. `gds_client.get_nodes_by_community(cid)` → node list
2. `build_community_prompt(cid, nodes)` → token-capped prompt
3. OpenAI chat completion using **fast model** (`gpt-4o-mini`)
4. `_upsert_to_chroma(summary)` → stores in `community_summaries` collection

---

## `community/global_rollup.py` — Global GraphRAG Rollup

**Purpose**: Microsoft GraphRAG hierarchical rollup — generates L2 and L3 summaries.

### `GlobalRollup`

#### `run_full_rollup() → dict`

1. Fetch all L1 community summaries from ChromaDB
2. Group into 11 domains via keyword heuristics
3. Generate L2 sub-system summaries via **fast model**
4. Generate L3 Global Architecture Document (one call via **strong model**)
5. Return `{l2_count, l3_generated, domains}`

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

---

## `reasoning/router.py` — Query Router

**Purpose**: Classifies every incoming question into one of four routes.

| Route | Trigger | LLM calls | Method |
|---|---|---|---|
| **A — SYMBOLIC** | Exact code symbol (e.g. `IMPERSONATED_SUBJECT`) | 0 | Ripgrep → Neo4j by FQN |
| **B — EXACT-ENTITY** | Named class/method (e.g. `TokenExchangeGrantHandler`) | 0 | Neo4j MATCH by name |
| **C — SEMANTIC** | Conceptual question | 1 (reduce step only) | ChromaDB vector search |
| **D — GLOBAL** | Architecture overview | 0 | Returns L3/L2 summaries directly |

Routes A and B are 100% deterministic — same question always returns evidence from same code.

---

## `reasoning/map_step.py` — Map Step

**Purpose**: First stage of GraphRAG reasoning. Retrieves and scores community summaries.

### `MapStep`

#### `run(input_text, restrict_community_ids, mode) → list[MapResult]`

| Mode | Trigger | LLM calls | Scoring method |
|---|---|---|---|
| `question` | Route C semantic queries | **0** | ChromaDB cosine distance → 0-100 score |
| `pr` | Blast-radius PR analysis | Up to 50 | `ThreadPoolExecutor` LLM scoring |

#### Question mode — ChromaDB distance scoring (zero LLM calls)

```python
results = collection.query(query_texts=[question], n_results=20, include=["distances", ...])
score = max(0, round((1.0 - distance / 2.0) * 100))
# Typical scores for broad queries: 40-57 (NOT 70-100)
# Do NOT apply LLM threshold (70) to question-mode results — all results would be filtered out
```

#### PR mode — LLM scoring

```python
n = min(max(n_candidates, total // 4), 50)   # capped at 50 candidates
# ThreadPoolExecutor calls _score_community() per candidate using fast model
```

### `MapResult`

`community_id`, `score` (0–100), `reason` (1 sentence), `summary_text`

### Score interpretation

| Range | Mode | Meaning |
|---|---|---|
| 70–100 | `pr` LLM | High blast-radius impact |
| 30–69 | `pr` LLM | Moderate impact |
| 50–57 | `question` ChromaDB | Top vector similarity hit |
| 40–49 | `question` ChromaDB | Relevant but not a close match |

---

## `reasoning/reduce_step.py` — Reduce Step

**Purpose**: Synthesises the final answer from Map results and evidence. Uses **strong model** (`gpt-4o`).

### `ReduceStep`

#### `run(map_results, query, primary_targets, code_snippets) → str`

1. `_detect_intent(query)` — classifies into 6 intents
2. `_build_summaries_text(map_results, budget)` — assembles community block text, trims from lowest-score end, always retains at least 1 entry
3. `_format_code_snippets(snippets, budget)`
4. Selects system prompt + template based on intent
5. Single LLM call via **strong model**

### Intent Detection

| Intent | Keywords | Output Tokens | System Prompt |
|---|---|---|---|
| `code` | `show me`, `give me`, `how is X implemented`, `source of` | 1 800 | `SYSTEM_IMPACT` |
| `safety` | `can I remove`, `is it safe`, `dead code`, `safe to delete` | 1 800 | `SYSTEM_SAFETY` |
| `capability` | `does this support`, `is X enforced`, `can X bypass` | 1 800 | `SYSTEM_CAPABILITY` |
| `impact` | `blast radius`, `what breaks`, `impact`, `dependency` | 1 800 | `SYSTEM_IMPACT` |
| `narrative` | `full story`, `end-to-end`, `how does X get evaluated`, `walk me through` | **6 000** | `SYSTEM_NARRATIVE` |
| `general` | *(fallback)* | **6 000** | `SYSTEM_GENERAL` |

### Token Budget Constants

| Constant | Value | Used for |
|---|---|---|
| `OUTPUT_RESERVE` | 1 800 | safety / capability / impact / code |
| `NARRATIVE_RESERVE` | 6 000 | narrative / general |
| `SNIPPET_BUDGET` | 50% of DATA_BUDGET | default code snippet budget |
| `SUMMARY_BUDGET` | 40% of DATA_BUDGET | default community summary budget |

---

## `reasoning/graph_retriever.py` — Neo4j Graph Retriever

**Purpose**: Given seed FQNs, traverses the call graph to find all affected code (blast radius). Also supports entity lookup by name, grep-to-graph bridging, and community summary fetching.

---

## `reasoning/lexical_search.py` — Lexical Searcher

**Purpose**: Runs ripgrep, git-grep, or pure-Python grep over `./mirror/` to find exact text matches. Returns file path + line number + matching line.

---

## `reasoning/code_fetcher.py` — Source Code Reader

**Purpose**: Given a file path and line numbers, reads the actual `.java` file from `./mirror/` and returns the source as a string for inclusion in LLM context.

---

## `reasoning/flow_summarizer.py` — Flow Narrative Summariser

**Purpose**: Converts `FlowPath` objects (API→DB execution paths) into natural-language architectural stories via **fast model**.

### `FlowNarrativeSummarizer`

#### `summarize_all(flow_paths) → list[FlowNarrative]`

Parallel LLM calls via `ThreadPoolExecutor`. Each narrative describes: business process, data transformations, services crossed, tables written to, config keys consulted.

### `FlowNarrative`

| Field | Description |
|---|---|
| `flow_id` | `"flow_{entry_hash}_{sink_hash}"` |
| `entry_fqn` | Starting API endpoint |
| `sink_fqn` | Terminal database class |
| `narrative_text` | GPT-4o-mini generated story |
| `path_length` | Number of nodes in the execution path |

---

## `sample_repos/repos.yaml` — Repository Configuration

```yaml
repos:
  - name: identity-oauth2-grant-token-exchange
    url: https://github.com/wso2-extensions/identity-oauth2-grant-token-exchange
    branch: main
  - name: identity-inbound-auth-oauth
    url: https://github.com/wso2-extensions/identity-inbound-auth-oauth
    branch: master
```

Each entry: `name` (used in GEID generation), `url` (git remote), `branch`.

---

## `docker-compose.yml` — Services

| Service | Image | Port | Purpose |
|---|---|---|---|
| `neo4j` | `neo4j:5.x` with APOC + GDS | 7474 (HTTP), 7687 (Bolt) | Property graph + Leiden + Dijkstra |
| `chromadb` | `ghcr.io/chroma-core/chroma:0.4.15` | 8000 | Vector store (6 collections) |
| `redis` | `redis:7` | 6379 | Pipeline state |

```bash
docker compose up -d      # start services
docker compose down -v    # stop + remove volumes (full reset)
```
