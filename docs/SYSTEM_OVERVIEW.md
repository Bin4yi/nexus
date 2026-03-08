# CodeNexus System Overview

This diagram represents the high-level architecture of the CodeNexus system, including the
ingestion pipelines, knowledge base building, reasoning engine, and storage layers.

```mermaid
graph TB
    %% Styling
    classDef client fill:#f4f4f4,stroke:#333,stroke-width:2px,color:#000
    classDef processing fill:#e1f5fe,stroke:#0288d1,stroke-width:2px,color:#000
    classDef reasoning fill:#e8f5e9,stroke:#388e3c,stroke-width:2px,color:#000
    classDef storage fill:#f3e5f5,stroke:#7b1fa2,stroke-width:2px,color:#000

    %% Client Layer
    subgraph ClientLayer ["Client Layer"]
        CLI["CLI / Python API (main.py)"]
        MCP["MCP Server (mcp_server.py) — Sprint 3"]
    end
    class ClientLayer,CLI,MCP client

    %% Main CodeNexus Engine
    subgraph Engine ["CodeNexus Orchestrator & Reasoning Host"]

        %% Ingestion Pipeline
        subgraph Pipelines ["Indexing & Parsing Pipelines"]
            direction TB
            mirror["Mirror Engine (mirror.py)<br/>Clones WSO2 Repositories"]
            parser["AST Parser (java_parser.py)<br/>Modifiers · Annotations (dict) · Generics<br/>OSGi Lifecycle · Lambda Calls"]
            config["Config Parser (config_parser.py)<br/>deployment.toml (tomllib) · XML · Properties"]
            linker["Linker Subsystem<br/>Maven Resolver · API Bridge<br/>JAX-RS Path Composition"]

            mirror --> parser
            parser --> linker
            parser --> config
        end
        class Pipelines,mirror,parser,config,linker processing

        %% Embedding & Graph Building
        subgraph SetupSubsystem ["Knowledge Base Builder"]
            direction TB
            chunker["Chunker Engine<br/>Code & Text Segmentation"]
            embedder["Embedding Subsystem<br/>all-MiniLM-L6-v2 (384-dim)"]
            loader["Graph Loader<br/>MERGE · visibility · lifecycle · annotations(JSON)"]

            linker --> chunker
            chunker --> embedder
            linker --> loader
            config --> loader
        end
        class SetupSubsystem,chunker,embedder,loader processing

        %% Reasoning Engine
        subgraph Reasoning ["Reasoning & Query Ecosystem"]
            direction TB
            router["Query Router<br/>Route A: Symbolic · B: Entity<br/>Route C: Semantic · D: Global"]
            map["Map Step (map_step.py)<br/>Question mode: 0 LLM calls<br/>PR mode: fast model scoring"]
            reduce["Reduce Step (reduce_step.py)<br/>Strong model · 6 intents<br/>Intent-adaptive token budget"]
            codefetcher["Graph/Code Retrievers<br/>(GDS Client · Code Fetcher · Lexical Search)"]

            router --> map
            map --> reduce
            reduce --> codefetcher
        end
        class Reasoning,router,map,reduce,codefetcher reasoning

    end

    %% Storage Layer
    subgraph StorageLayer ["Storage & State Layer"]
        direction LR
        FS[("Local File System<br/>(mirror/ java repos)")]
        Chroma[("ChromaDB<br/>code_logic · code_intent<br/>community_summaries<br/>flow_narratives · l2/l3 summaries")]
        Neo4j[("Neo4j + GDS<br/>16 edge types · Leiden<br/>visibility · lifecycle props<br/>Specification nodes (Sprint 2)")]
        Redis[("Redis<br/>Pipeline State")]
    end
    class StorageLayer,FS,Chroma,Neo4j,Redis storage

    %% LLM integration
    subgraph External ["External Services"]
        LLM_Fast["OpenAI gpt-4o-mini (fast)<br/>Community summaries · Map scoring<br/>Query expansion · L2 rollup"]
        LLM_Strong["OpenAI gpt-4o (strong)<br/>Final reduce answer · L3 global rollup"]
    end
    class External,LLM_Fast,LLM_Strong storage

    %% Inter-layer Connections
    CLI -->|orchestrate()| mirror
    CLI -->|query()| router
    MCP -->|tool calls| router
    MCP -->|blast_radius()| codefetcher

    mirror -.->|read/write| FS
    parser -.->|parse files| FS
    codefetcher -.->|fetch context| FS

    embedder -->|cosine search/store| Chroma
    loader -->|cypher MERGE/nodes| Neo4j
    loader -.->|stage tracking| Redis

    map <-->|distances / fetch| Chroma
    codefetcher <-->|graph walks & paths| Neo4j

    reduce <-->|inference| LLM_Strong
    map <-->|scoring| LLM_Fast
    embedder <-->|tokenization| LLM_Fast
```

---

## Pipeline Stages

```
Stage 1 — Mirror
  pipeline/mirror.py          → git clone/pull → ./mirror/

Stage 2 — Extract
  parsers/java_parser.py      → Tree-sitter AST → UIR (classes, methods, modifiers, annotations)
  parsers/config_parser.py    → deployment.toml, XML, properties → ConfigurationInfo

Stage 3 — Link
  linker/maven_resolver.py    → pom.xml → DEPENDS_ON edges
  linker/api_bridge.py        → @Path + @RequestMapping → REMOTE_CALLS edges (with path composition)

Stage 4 — Load
  graph/loader.py             → Neo4j MERGE (nodes + 16 edge types + visibility/lifecycle props)
  vectorstore/embedder.py     → ChromaDB upsert (code_logic + code_intent collections)

Stage 5 — Post-Processing
  graph/gds_client.py         → Leiden community detection (GDS)
  community/summarizer.py     → GPT-4o-mini summaries → community_summaries collection
  graph/tagger.py             → :EntryPoint / :DataSink labels
  graph/flow_extractor.py     → GDS Dijkstra → FlowPath objects
  reasoning/flow_summarizer.py→ GPT-4o-mini flow narratives → flow_narratives collection
  community/global_rollup.py  → L2 (fast model) + L3 (strong model) → ChromaDB
```

---

## Two-Tier LLM Model Strategy

| Tier | Default Model | Config Env Var | Where Used |
|---|---|---|---|
| **Fast** | `gpt-4o-mini` | `LLM_FAST_MODEL` | Community summarisation · map scoring · L2 rollup · query expansion |
| **Strong** | `gpt-4o` | `LLM_STRONG_MODEL` | Final reduce answer · L3 global rollup only |

All callers use `settings.make_llm_client(tier="fast")` or `settings.make_llm_client(tier="strong")`.
Switching between OpenAI and Azure requires only `.env` changes — no code changes.

---

## v2 Sprint Roadmap

| Sprint | Focus | Status |
|---|---|---|
| **Sprint 1** | Parser accuracy: visibility/modifiers, annotation dicts, JAX-RS path composition, OSGi lifecycle, tomllib, lambda calls | ✅ Complete |
| **Sprint 2** | RFC Specification Knowledge Base: IETF RFC fetcher, citation scanner, `Specification` nodes, `IMPLEMENTS_SPEC` edges | Planned |
| **Sprint 3** | MCP Server: `query_codebase`, `blast_radius`, `audit_spec_compliance`, `recall_session` tools | Planned |
| **Sprint 4** | Query intelligence: query expansion, cross-encoder re-ranking, multiprocessing parser, community fingerprint cache | Planned |
