# CodeNexus System Overview

This diagram represents the high-level architecture of the CodeNexus system, including the ingestion pipelines, knowledge base building, reasoning engine, and storage layers.

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
    end
    class ClientLayer,CLI client

    %% Main CodeNexus Engine
    subgraph Engine ["CodeNexus Orchestrator & Reasoning Host"]
        
        %% Ingestion Pipeline
        subgraph Pipelines ["Indexing & Parsing Pipelines"]
            direction TB
            mirror["Mirror Engine (mirror.py)<br/>Clones WSO2 Repositories"]
            parser["AST & Specialized Parsers<br/>(Java, SQL, Config, Javadoc)"]
            linker["Linker Subsystem<br/>(Maven Resolver, API Bridge)"]
            
            mirror --> parser
            parser --> linker
        end
        class Pipelines,mirror,parser,linker processing

        %% Embedding & Graph Building
        subgraph SetupSubsystem ["Knowledge Base Builder"]
            direction TB
            chunker["Chunker Engine<br/>Code & Text Segmentation"]
            embedder["Embedding Subsystem<br/>Provider Router"]
            loader["Graph Loader & Extractor<br/>(Schema, Flow Extractor)"]
            
            linker --> chunker
            chunker --> embedder
            linker --> loader
        end
        class SetupSubsystem,chunker,embedder,loader processing

        %% Reasoning Engine
        subgraph Reasoning ["Reasoning & Query Ecosystem"]
            direction TB
            router["Query Router<br/>Semantic / Lexical Decision"]
            map["Map Step (pipeline.py)<br/>Dual-mode Scoring (Question/PR)"]
            reduce["Reduce Step (Synthesis)<br/>Intent Classification (6 Intents)"]
            codefetcher["Graph/Code Retrievers<br/>(GDS Client, Code Fetcher)"]
            
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
        Chroma[("ChromaDB<br/>(Vector Embeddings & Chunks)")]
        Neo4j[("Neo4j Database<br/>(AST Graph + GDS Algorithms)")]
    end
    class StorageLayer,FS,Chroma,Neo4j storage

    %% LLM integration
    subgraph External ["External Services"]
        LLM["Azure OpenAI (GPT-5)<br/>LLM Client Factory"]
    end
    class External,LLM storage

    %% Inter-layer Connections
    CLI -->|orchestrate()| mirror
    CLI -->|query()| router

    mirror -.->|read/write| FS
    parser -.->|parse files| FS
    codefetcher -.->|fetch context| FS

    embedder -->|cosine search/store| Chroma
    loader -->|cypher/nodes| Neo4j

    map <-->|distances / fetch| Chroma
    codefetcher <-->|graph walks & paths| Neo4j
    
    reduce <-->|inference| LLM
    embedder <-->|tokenization| LLM

```
