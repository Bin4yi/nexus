# CodeNexus System Overview

This diagram represents the high-level architecture of the CodeNexus v2 system, including
all four sprint improvements: high-speed ingestion, infrastructure linkers, weighted graph
routing, and the intelligence layer.

```mermaid
graph TB
    %% Styling
    classDef client fill:#f4f4f4,stroke:#333,stroke-width:2px,color:#000
    classDef processing fill:#e1f5fe,stroke:#0288d1,stroke-width:2px,color:#000
    classDef reasoning fill:#e8f5e9,stroke:#388e3c,stroke-width:2px,color:#000
    classDef storage fill:#f3e5f5,stroke:#7b1fa2,stroke-width:2px,color:#000
    classDef local fill:#fff8e1,stroke:#f9a825,stroke-width:2px,color:#000

    %% Client Layer
    subgraph ClientLayer ["Client Layer"]
        CLI["CLI / Python API (main.py)"]
        MCP["MCP Server (mcp_server.py) — planned"]
    end
    class ClientLayer,CLI,MCP client

    %% Main CodeNexus Engine
    subgraph Engine ["CodeNexus Orchestrator & Reasoning Host"]

        %% Ingestion Pipeline
        subgraph Pipelines ["Indexing & Parsing Pipelines (Sprint 1–2)"]
            direction TB
            mirror["Mirror Engine (mirror.py)<br/>Clones Repositories"]
            parser["AST Parser (java_parser.py)<br/>Modifiers · Annotations (dict) · Generics<br/>OSGi Lifecycle · Lambda Calls"]
            ingest["Parallel Ingest (pipeline/ingest.py)<br/>ProcessPoolExecutor · CPU-parallel<br/>Sliding Window Chunker"]
            config["Config Parser (config_parser.py)<br/>deployment.toml (tomllib) · XML · Properties"]
            osgi["OSGi Resolver (parsers/osgi_parser.py)<br/>@Component · @Reference → RESOLVES_TO"]
            rfc["RFC Parser (parsers/rfc_parser.py)<br/>IETF RFC files → Specification nodes<br/>Java citations → IMPLEMENTS_SPEC edges"]
            linker["Linker Subsystem<br/>Maven Resolver · API Bridge<br/>JAX-RS Path Composition"]

            mirror --> parser
            parser --> ingest
            parser --> linker
            parser --> config
            parser --> osgi
            parser --> rfc
        end
        class Pipelines,mirror,parser,ingest,config,osgi,rfc,linker processing

        %% Embedding & Graph Building
        subgraph SetupSubsystem ["Knowledge Base Builder"]
            direction TB
            chunker["Chunker Engine (vectorstore/chunker.py)<br/>Sliding Window · [Package:][Class:] prefix<br/>CHUNK_SIZE=512 · OVERLAP=128"]
            embedder["Embedding Subsystem (vectorstore/embedder.py)<br/>ChromaEmbedder: all-MiniLM-L6-v2 (CPU)<br/>FastEmbedder: nomic-embed-text-v1.5 (GPU opt-in)"]
            loader["Graph Loader (graph/loader.py)<br/>MERGE · visibility · lifecycle · annotations(JSON)<br/>RESOLVES_TO · IMPLEMENTS_SPEC edges"]

            ingest --> chunker
            chunker --> embedder
            linker --> loader
            config --> loader
            osgi --> loader
            rfc --> loader
        end
        class SetupSubsystem,chunker,embedder,loader processing

        %% Post-Processing
        subgraph PostProc ["Post-Processing (Sprint 3–4)"]
            direction TB
            leiden["Leiden Community Detection (GDS)"]
            summarizer["Community Summarizer<br/>gpt-4o-mini (fast model)"]
            local_draft["Local Micro-Draft Engine (llm/local_drafting.py)<br/>Ollama llama3.2 — $0 cost<br/>Stores micro_draft on Component nodes"]
            tagger["Node Tagger (graph/tagger.py)<br/>:EntryPoint · :DataSink labels"]
            dijkstra["Flow Extractor (graph/flow_extractor.py)<br/>GDS Dijkstra — CALLS=1 · REMOTE_CALLS=5<br/>maxDepth=15 · FlowPath objects"]
            flow_sum["Flow Narrative Summarizer<br/>gpt-4o-mini — End-to-End Stories"]
            rollup["Global GraphRAG Rollup<br/>L2 Sub-Systems (fast) + L3 Global Arch (strong)"]

            loader --> leiden
            leiden --> summarizer
            leiden --> tagger
            tagger --> dijkstra
            dijkstra --> flow_sum
            summarizer --> rollup
        end
        class PostProc,leiden,summarizer,tagger,dijkstra,flow_sum,rollup processing
        class local_draft local

        %% Reasoning Engine
        subgraph Reasoning ["Reasoning & Query Ecosystem"]
            direction TB
            router["Query Router (reasoning/router.py)<br/>Route A: Symbolic · B: Entity<br/>Route C: Semantic · D: Global"]
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
        FS[("Local File System<br/>(mirror/ java repos · rfcs/)")]
        Chroma[("ChromaDB<br/>code_logic · code_intent<br/>community_summaries<br/>flow_narratives · l2/l3 summaries")]
        Neo4j[("Neo4j + GDS<br/>19 edge types · Leiden · Dijkstra<br/>visibility · lifecycle · micro_draft<br/>Specification nodes · RESOLVES_TO")]
        Redis[("Redis<br/>Pipeline State")]
    end
    class StorageLayer,FS,Chroma,Neo4j,Redis storage

    %% LLM integration
    subgraph External ["External Services"]
        LLM_Fast["OpenAI gpt-4o-mini (fast)<br/>Community summaries · Map scoring<br/>Flow narratives · L2 rollup"]
        LLM_Strong["OpenAI gpt-4o (strong)<br/>Final reduce answer · L3 global rollup"]
    end
    class External,LLM_Fast,LLM_Strong storage

    subgraph LocalLLM ["Local LLM (opt-in, $0 cost)"]
        Ollama["Ollama (llm/local_drafting.py)<br/>llama3.2:3b — EntryPoint micro-drafts"]
    end
    class LocalLLM,Ollama local

    %% Inter-layer Connections
    CLI -->|orchestrate()| mirror
    CLI -->|query()| router

    mirror -.->|read/write| FS
    parser -.->|parse files| FS
    rfc -.->|read RFC files| FS
    codefetcher -.->|fetch context| FS

    embedder -->|cosine search/store| Chroma
    loader -->|cypher MERGE/nodes| Neo4j
    loader -.->|stage tracking| Redis

    map <-->|distances / fetch| Chroma
    codefetcher <-->|graph walks & paths| Neo4j

    reduce <-->|inference| LLM_Strong
    map <-->|scoring| LLM_Fast
    flow_sum <-->|narratives| LLM_Fast
    rollup <-->|summarize| LLM_Fast
    rollup <-->|global arch| LLM_Strong

    local_draft <-->|micro-drafts| Ollama
```

---

## Pipeline Stages

```
Stage 1 — Mirror
  pipeline/mirror.py          → git clone/pull → ./mirror/

Stage 2 — Extract (Sprint 1: parallel)
  pipeline/ingest.py          → ProcessPoolExecutor → parallel Java parsing + chunking
  parsers/java_parser.py      → Tree-sitter AST → UIR (classes, methods, modifiers, annotations)
  parsers/config_parser.py    → deployment.toml, XML, properties → ConfigurationInfo
  parsers/osgi_parser.py      → @Component/@Reference → OSGiComponentInfo + OSGiResolutionEdge
  parsers/rfc_parser.py       → RFC markdown files → SpecificationInfo + SpecImplementsEdge

Stage 3 — Link
  linker/maven_resolver.py    → pom.xml → DEPENDS_ON edges
  linker/api_bridge.py        → @Path + @RequestMapping → REMOTE_CALLS edges

Stage 4 — Load
  graph/loader.py             → Neo4j MERGE (nodes + 19 edge types + visibility/lifecycle/spec props)
  vectorstore/embedder.py     → ChromaDB upsert (sliding window chunks, optional GPU via FastEmbedder)

Stage 5 — Post-Processing
  graph/gds_client.py         → Leiden community detection (GDS)
  community/summarizer.py     → GPT-4o-mini summaries → community_summaries collection
  graph/tagger.py             → :EntryPoint / :DataSink labels
  graph/flow_extractor.py     → Weighted GDS Dijkstra (CALLS=1, REMOTE_CALLS=5) → FlowPath objects
  reasoning/flow_summarizer.py→ GPT-4o-mini flow narratives → flow_narratives collection
  llm/local_drafting.py       → Ollama micro-drafts for EntryPoint classes (opt-in, $0)
  community/global_rollup.py  → L2 (fast model) + L3 (strong model) → ChromaDB
```

---

## Two-Tier LLM Model Strategy

| Tier | Default Model | Config Env Var | Where Used |
|---|---|---|---|
| **Fast** | `gpt-4o-mini` | `LLM_FAST_MODEL` | Community summarisation · map scoring · flow narratives · L2 rollup |
| **Strong** | `gpt-4o` | `LLM_STRONG_MODEL` | Final reduce answer · L3 global rollup only |
| **Local** | `llama3.2:3b` | `OLLAMA_MODEL` | EntryPoint micro-drafts (opt-in, $0 cost) |

All cloud callers use `settings.make_llm_client(tier="fast")` or `settings.make_llm_client(tier="strong")`.
Switching between OpenAI and Azure requires only `.env` changes.

---

## Sprint Delivery Status

| Sprint | Focus | Status |
|---|---|---|
| **Sprint 1** | High-Speed Engine: sliding window chunker, GPU embeddings (nomic), ProcessPoolExecutor parallel parser | ✅ Complete |
| **Sprint 2** | Infrastructure Linkers: OSGi @Component/@Reference resolver, SQL tables, config files | ✅ Complete |
| **Sprint 3** | Weighted Dijkstra: CALLS=1 / REMOTE_CALLS=5 edge weights, :EntryPoint/:DataSink tagging, maxDepth=15 | ✅ Complete |
| **Sprint 4** | Intelligence Layer: RFC specification grounding, Ollama micro-drafts, flow narratives, L2/L3 rollup | ✅ Complete |
