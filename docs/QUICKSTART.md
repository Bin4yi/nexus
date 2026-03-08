# CodeNexus — Quick Start Guide

Get from zero to asking questions about your Java codebase in four steps.

---

## Prerequisites

| Requirement | Version | Notes |
|---|---|---|
| Python | 3.11+ | `py --version` on Windows |
| Docker Desktop | 24+ | Must be running before Step 1 |
| OpenAI API key | — | `gpt-4o-mini` + `gpt-4o` access |
| Git | any | For cloning repos into the mirror |

---

## Step 1 — Start the databases

```bash
cd nexus
docker compose up -d
```

This starts three containers:

| Container | Port | Purpose |
|---|---|---|
| `nexus-neo4j` | 7687 / 7474 | Graph database (structural truth) |
| `nexus-chromadb` | 8000 | Vector store (semantic search) |
| `nexus-redis` | 6379 | Pipeline state tracking |

Wait ~60 seconds for Neo4j to finish initialising. Check readiness:

```bash
docker compose ps          # all three should show "(healthy)"
```

Neo4j browser (optional): open `http://localhost:7474` — login `neo4j / nexuspassword`.

---

## Step 2 — Configure environment

Copy the example file and fill in your API key:

```bash
cp .env.example .env
```

Open `.env` and set:

```bash
# Required — your OpenAI key
LLM_API_KEY=sk-...

# Model tiers (defaults are fine)
LLM_FAST_MODEL=gpt-4o-mini      # bulk ops: summarisation, scoring
LLM_STRONG_MODEL=gpt-4o         # final answers, global rollup

# Repos to ingest (points to sample_repos/repos.yaml by default)
REPOS_CONFIG_PATH=./sample_repos/repos.yaml
```

Everything else in `.env` can stay at its default for a first run.

---

## Step 3 — Add your repositories

Edit `sample_repos/repos.yaml` to list the repos you want to ingest:

```yaml
repositories:
  - name: carbon-identity-framework
    url: https://github.com/wso2/carbon-identity-framework.git
    branch: master
    language: java

  - name: identity-inbound-auth-oauth
    url: https://github.com/wso2-extensions/identity-inbound-auth-oauth.git
    branch: master
    language: java
```

> Each `name` must be unique — it becomes the repository identifier in the graph.

---

## Step 4 — Install dependencies and ingest

```bash
# Install Python dependencies (one-time)
py -m pip install -r requirements.txt

# Run the full ingestion pipeline
py main.py ingest
```

The pipeline runs five stages automatically:

```
1. Mirror    — git clone / git pull all repos into ./mirror/
2. Extract   — Tree-sitter AST → UIR objects (methods, classes, annotations)
3. Link      — Maven dependencies + REST API bridge + JAX-RS path composition
4. Load      — MERGE nodes into Neo4j, embed method bodies into ChromaDB
5. Post      — Leiden communities → LLM summaries → flow extraction → global rollup
```

Ingestion time for reference:

| Repos | Java files | Approx. time |
|---|---|---|
| 1 small repo | ~500 files | 5–15 min |
| 2–3 medium repos | ~5,000 files | 15–40 min |
| 5 large repos | ~20,000+ files | 30–90 min |

The main time cost is community summarisation (LLM API calls). Increase parallelism to speed it up:

```bash
SUMMARIZER_MAX_WORKERS=16     # in .env
```

---

## Querying the knowledge base

### Interactive mode (recommended)

Start a continuous REPL — ask as many questions as you like without restarting:

```bash
py main.py interactive
```

```
╔══════════════════════════════════════════════════════════════════╗
║          CodeNexus — Interactive Query Mode                      ║
║  Type a question and press Enter.  'exit' or Ctrl+C to quit.    ║
╚══════════════════════════════════════════════════════════════════╝

  nexus> how does the OAuth token exchange flow work?
  nexus> which classes implement the TokenValidator interface?
  nexus> exit
```

### Single question

```bash
py main.py query "is impersonation supported without an actor token?"
```

### Explain a class or method

```bash
py main.py explain OAuthAdminService
py main.py explain validateActorToken
```

### Trace a call chain

```bash
py main.py trace TokenExchangeGrantHandler --depth 3
```

### Find every caller of a method

```bash
py main.py callers isImpersonationRequest --code
```

### Exact text search (no LLM, instant)

```bash
py main.py find "throw new OAuthSystemException"
```

### Debug an exception

```bash
py main.py debug NullPointerException
py main.py debug "$(cat stacktrace.txt)"
```

---

## Incremental updates (after code changes)

You do not need to re-ingest everything when repos change. The incremental updater:
- detects only the files changed since the last git SHA
- re-parses and re-loads only those files
- re-runs Leiden and re-summarises only the affected communities

```bash
py main.py ingest      # safe to re-run at any time — unchanged files are skipped
```

A small PR (10 files changed across 5 repos) re-ingests in **under 5 minutes**.

---

## Azure OpenAI (optional)

If you use Azure instead of OpenAI, set in `.env`:

```bash
LLM_PROVIDER=azure
AZURE_OPENAI_ENDPOINT=https://your-resource.openai.azure.com/
AZURE_OPENAI_API_KEY=your-azure-key
AZURE_OPENAI_API_VERSION=2024-08-01-preview
LLM_FAST_MODEL=gpt-4o-mini        # your Azure deployment name
LLM_STRONG_MODEL=gpt-4o           # your Azure deployment name
```

No code changes needed — the `make_llm_client(tier)` factory handles the switch.

---

## Key ports and UIs

| Service | URL | Credentials |
|---|---|---|
| Neo4j Browser | `http://localhost:7474` | neo4j / nexuspassword |
| ChromaDB API | `http://localhost:8000/api/v1` | — |
| Redis | `localhost:6379` | — |

---

## Stopping the databases

```bash
docker compose down          # stop containers, keep data volumes
docker compose down -v       # stop and DELETE all data (full reset)
```

---

## Common issues

| Symptom | Fix |
|---|---|
| `Connection refused` on Neo4j | Wait 60 s after `docker compose up`; check `docker compose ps` shows `(healthy)` |
| `ModuleNotFoundError` | Run `py -m pip install -r requirements.txt` inside the `nexus/` directory |
| Ingestion stuck at summarisation | Increase `SUMMARIZER_MAX_WORKERS` or check OpenAI rate limits |
| Empty query results | Check Neo4j has nodes: run `MATCH (n) RETURN count(n)` in the browser |
| `tomllib` not found | Requires Python 3.11+. Run `py --version` to confirm |

---

## What's next

| Task | Command / file |
|---|---|
| Add more repos | Edit `sample_repos/repos.yaml`, re-run `py main.py ingest` |
| Tune query depth | `py main.py query "..." --depth 5` |
| Scope to one repo | `py main.py interactive --repo carbon-identity-framework` |
| Read full architecture | `docs/ARCHITECTURE.md` |
| Read all CLI options | `docs/CODE_REFERENCE.md` |
