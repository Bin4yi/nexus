# CodeNexus — Azure Deployment Cost Breakdown

> **Last updated:** March 2026
> All prices are **Azure East US 2** region (where the OpenAI endpoint is already deployed).
> Prices in USD. Azure pricing fluctuates — verify at [azure.microsoft.com/pricing](https://azure.microsoft.com/en-us/pricing/).

---

## Table of Contents

1. [Architecture Components](#1-architecture-components)
2. [Deployment Tiers](#2-deployment-tiers)
3. [Tier A — Personal / Proof of Concept](#tier-a--personal--proof-of-concept)
4. [Tier B — Small Team (5–10 devs)](#tier-b--small-team-510-devs)
5. [**Tier B+ — 100 Repos, Small Team (5–10 devs)**](#tier-b--100-repos-small-team-510-devs)
6. [Tier C — Medium Team (25–50 devs)](#tier-c--medium-team-2550-devs)
7. [Tier D — Large Scale (100 devs / 100 repos)](#tier-d--large-scale-100-devs--100-repos)
8. [Azure OpenAI Cost Deep Dive](#8-azure-openai-cost-deep-dive)
9. [Cost Optimisation Strategies](#9-cost-optimisation-strategies)
10. [Recommended Starting Point](#10-recommended-starting-point)
11. [vs. Commercial Alternatives](#11-vs-commercial-alternatives)

---

## 1. Architecture Components

CodeNexus requires five running services:

| Service | Role | Scaling Bottleneck |
|---|---|---|
| **Neo4j** | Graph database — nodes, relationships, Cypher queries | RAM (page cache) |
| **ChromaDB** | Vector store — semantic embeddings | RAM (vector index) |
| **Redis** | Query cache, session state | Negligible |
| **Python API** | FastAPI server, retrieval pipeline, embedding model | CPU (concurrent queries) |
| **Azure OpenAI** | LLM inference (gpt-4o-mini + gpt-5) | Pay-per-token (external) |

> **Note:** Azure OpenAI is an external managed service — no VM sizing required for it.
> It is billed purely per token consumed and is the dominant cost at scale.

---

## 2. Deployment Tiers

| Tier | Developers | Repos | Queries/day | Recommended VM |
|---|---|---|---|---|
| A | 1–2 | 1–5 | ~20 | Single B-series VM |
| B | 5–10 | 5–15 | ~100 | Single D/E-series VM |
| C | 25–50 | 20–50 | ~500 | 2 VMs (split Neo4j + app) |
| D | 100+ | 100+ | ~2,000 | Managed services required |

---

## Tier A — Personal / Proof of Concept

**Target:** 1–2 developers, current WSO2 IS repo set (4 repos, ~28k methods).

### VM: Standard_E4s_v3

| Spec | Value |
|---|---|
| vCPUs | 4 |
| RAM | 32 GB |
| Temp SSD | 64 GB |

### Monthly Cost Breakdown

| Item | SKU | Cost/mo |
|---|---|---|
| VM compute (on-demand) | Standard_E4s_v3 | $252 |
| Managed SSD (128 GB, P10) | Premium SSD LRS | $19 |
| Public IP (static) | Standard SKU | $4 |
| Outbound bandwidth (est. 50 GB) | Zone-to-internet | $4 |
| **Infrastructure subtotal** | | **$279/mo** |

### Azure OpenAI (Tier A query volume)

| Model | Usage | Tokens/mo | Cost/mo |
|---|---|---|---|
| gpt-4o (final answers) | 20 queries/day × 30k input + 3k output | 19.8M input / 1.98M output | $49 + $20 = **$69** |
| gpt-4o-mini (parser, bulk) | 20 queries/day × 2k tokens | 1.2M | **$0.24** |

| **Total Tier A** | | **~$350/mo** |
|---|---|---|

> **Per developer cost:** ~$175–350/mo depending on whether it is 1 or 2 developers.
> Equivalent to one mid-level consultant day. Justifiable for a complex legacy codebase.

---

## Tier B — Small Team (5–10 devs)

**Target:** Engineering team actively analysing WSO2 IS (current realistic use case).
10 repos, ~80k methods, 100 queries/day.

### VM: Standard_E4s_v3 (same VM, fits comfortably)

| Item | SKU | Cost/mo |
|---|---|---|
| VM compute | Standard_E4s_v3 | $252 |
| Managed SSD (256 GB, P15) | Premium SSD LRS | $38 |
| Public IP | Standard SKU | $4 |
| Outbound bandwidth (est. 100 GB) | Zone-to-internet | $8 |
| **Infrastructure subtotal** | | **$302/mo** |

### Azure OpenAI (Tier B query volume)

| Model | Usage | Tokens/mo | Cost/mo |
|---|---|---|---|
| gpt-4o (final answers) | 100 queries/day × 30k in + 3k out | 99M input / 9.9M output | $248 + $99 = **$347** |
| gpt-4o-mini (parser + bulk) | 100 queries/day × 2k tokens | 6M | **$1.20** |

| **Total Tier B** | | **~$650/mo** |
|---|---|---|

**Per developer (10 devs): ~$65/mo**

> Compare: GitHub Copilot is $19/user/mo but cannot answer architectural cross-repo questions.
> The $46 premium buys deep codebase understanding for senior/architect-level work.

### Cost if using gpt-5 for final answers (current `.env` setting)

| Model | Estimated price | Cost/mo (100 queries/day) |
|---|---|---|
| gpt-5 input | ~$15/1M tokens | ~$1,485 |
| gpt-5 output | ~$60/1M tokens | ~$594 |
| **LLM subtotal** | | **~$2,079/mo** |
| **Total with gpt-5** | | **~$2,381/mo** ($238/dev) |

> **Recommendation:** Use gpt-4o for final answers unless you specifically need gpt-5's reasoning depth.
> Set `LLM_QUERY_MODEL=gpt-4o` in `.env` to switch. Quality difference for code Q&A is marginal.

---

## Tier B+ — 100 Repos, Small Team (5–10 devs)

**Target:** Your realistic next step — ingest the full WSO2 ecosystem (100 repos) but keep the team small.
The cost driver here is **data size**, not headcount. 100 repos means ~3M graph nodes and ~3M ChromaDB vectors.

### What 100 Repos Looks Like in Numbers

| Metric | Current (4 repos) | 100 Repos |
|---|---|---|
| LogicUnit nodes (methods) | ~28k | ~700k–1M |
| Total Neo4j nodes | ~80k | ~3–5M |
| Total Neo4j relationships | ~200k | ~8–15M |
| ChromaDB vectors | ~32k | ~800k–1M |
| Graph on disk | ~500 MB | ~20–35 GB |
| Repo mirrors (git clones) | ~3 GB | ~50–100 GB |

### Why This Changes VM Requirements

With 3–5M nodes, Neo4j's page cache must be large enough that Cypher traversals don't hit disk on every query:

| RAM | Page Cache Available | Fits 100-repo graph? |
|---|---|---|
| 16 GB | ~8 GB | No — constant disk I/O, very slow queries |
| 32 GB | ~16 GB | Partial fit — acceptable for light use |
| 64 GB | ~48 GB | **Yes — full graph fits, fast queries** |
| 128 GB | ~96 GB | Comfortable headroom for growth |

### Infrastructure: 2 VMs Required (Neo4j must be isolated)

| VM | Role | SKU | Cost/mo |
|---|---|---|---|
| **VM-1 (Neo4j)** | Graph database | Standard_E8s_v3 (8 vCPU / 64 GB) | $504 |
| **VM-2 (App)** | ChromaDB + Redis + Python API | Standard_D4s_v3 (4 vCPU / 16 GB) | $192 |
| Managed SSD — VM-1 (512 GB, P20) | Neo4j data + repo mirrors | Premium SSD LRS | $69 |
| Managed SSD — VM-2 (256 GB, P15) | ChromaDB index + OS | Premium SSD LRS | $38 |
| Public IPs + bandwidth (est. 100 GB) | | | $16 |
| **Infrastructure subtotal** | | | **$819/mo** |

> If query latency is too slow on E8s_v3, upgrade Neo4j VM to **Standard_E16s_v3 (128 GB) at $1,008/mo**.
> The extra RAM allows the full 100-repo graph to fit entirely in the page cache.

### Azure OpenAI (same query volume as Tier B — 5–10 devs)

Query volume does not change with repo count — it depends on how many developers are querying.
100 queries/day is still the right estimate for 5–10 devs.

| Model | Tokens/mo | Cost/mo |
|---|---|---|
| gpt-4o input (100/day × 30k) | 99M | $248 |
| gpt-4o output (100/day × 3k) | 9.9M | $99 |
| gpt-4o-mini (parser + bulk) | 6M | $1.20 |
| **LLM subtotal** | | **$348/mo** |

### Total Tier B+

| Component | Cost/mo |
|---|---|
| Infrastructure (2 VMs + disks) | $819 |
| Azure OpenAI (gpt-4o) | $348 |
| **Total** | **~$1,170/mo** |
| Per developer (10 devs) | **~$117/mo** |

> With gpt-5 for final answers: add ~$1,700/mo in LLM costs → **~$2,870/mo total**.
> Switch to gpt-4o (`LLM_QUERY_MODEL=gpt-4o`) — the quality difference for code Q&A is negligible.

### Ingestion Time Warning (100 repos, one-time)

| Stage | Estimated Duration |
|---|---|
| Stage 1: git clone 100 repos | 2–6 hours (network bound) |
| Stage 2: Tree-sitter parse ~1M Java files | 4–12 hours (CPU bound) |
| Stage 3: Link graph edges | 2–4 hours |
| Stage 4: Load Neo4j + ChromaDB | 6–18 hours |
| Post: Leiden community detection | 2–4 hours |
| Post: Community summarisation (LLM) | 8–24 hours ($30–80 in gpt-4o-mini costs) |
| **Total first-time ingestion** | **~24–68 hours** |

Run ingestion with the VM running 24/7 for the first pass. Subsequent incremental ingests (adding 1–2 repos or syncing git changes) take 1–4 hours.

---

## Tier C — Medium Team (25–50 devs)

**Target:** Department-wide rollout, 20–50 repos, ~500 queries/day.

### Infrastructure: Split into 2 VMs

| VM | Role | SKU | Cost/mo |
|---|---|---|---|
| VM-1 (Neo4j) | Graph database only | Standard_E8s_v3 (8c/64GB) | $504 |
| VM-2 (App) | ChromaDB + Redis + API | Standard_D4s_v3 (4c/16GB) | $192 |
| Managed SSD × 2 (512 GB each) | P20 Premium SSD LRS | $69 × 2 | $138 |
| Public IPs + bandwidth | | | $20 |
| **Infrastructure subtotal** | | | **$854/mo** |

### Azure OpenAI (Tier C query volume)

| Model | Tokens/mo | Cost/mo |
|---|---|---|
| gpt-4o input (500 queries/day × 30k) | 450M | $1,125 |
| gpt-4o output (500 queries/day × 3k) | 45M | $450 |
| gpt-4o-mini (parser) | 30M | $6 |
| **LLM subtotal** | | **$1,581/mo** |

| **Total Tier C** | | **~$2,435/mo** |
|---|---|---|

**Per developer (50 devs): ~$49/mo**

---

## Tier D — Large Scale (100 devs / 100 repos)

**Target:** Enterprise-wide, 100+ repos, ~2,000 queries/day.
At this scale, single-VM Neo4j becomes the architectural bottleneck.

### Infrastructure: Managed Services Required

| Service | Azure SKU | Cost/mo |
|---|---|---|
| Neo4j AuraDB Enterprise | Professional tier (cloud-managed) | ~$2,000–5,000 |
| OR: Standard_M32ms VM (32c/875GB RAM) | Self-managed Neo4j | $4,537 |
| Azure Cache for Redis (C2 Standard) | Managed Redis | $101 |
| App Service Plan (P3v3 × 2 instances) | Python API, auto-scale | $820 |
| Azure Storage (2 TB) | Repo mirrors + data | $41 |
| Bandwidth + IPs | | $80 |
| **Infrastructure subtotal** | | **~$3,000–7,000/mo** |

### Azure OpenAI (Tier D query volume)

| Model | Tokens/mo | Cost/mo |
|---|---|---|
| gpt-4o input (2,000/day × 30k) | 1.8B | $4,500 |
| gpt-4o output (2,000/day × 3k) | 180M | $1,800 |
| gpt-4o-mini | 120M | $24 |
| **LLM subtotal** | | **$6,324/mo** |

| **Total Tier D** | | **~$9,000–13,000/mo** |
|---|---|---|

**Per developer (100 devs): ~$90–130/mo**

> At this scale, ChromaDB must be replaced with **Qdrant** or **Azure Cognitive Search**
> as ChromaDB is not designed for multi-million vector production workloads.

---

## 7. Azure OpenAI Cost Deep Dive

### Model Pricing Reference (Azure, East US 2)

| Model | Input (per 1M tokens) | Output (per 1M tokens) | Best for |
|---|---|---|---|
| gpt-4o-mini | $0.15 | $0.60 | Parser, bulk ops, community summaries |
| gpt-4o | $2.50 | $10.00 | Final answers (recommended default) |
| gpt-5 (o3-class) | ~$15–75 | ~$60–150 | Complex reasoning only |

### Per-Query Cost Estimate

A typical CodeNexus query assembles ~30k context tokens and produces ~3k output tokens:

| Final Answer Model | Input Cost | Output Cost | **Total per Query** |
|---|---|---|---|
| gpt-4o-mini | $0.0045 | $0.0018 | **$0.006** |
| gpt-4o | $0.075 | $0.030 | **$0.105** |
| gpt-5 | $0.450 | $0.450 | **$0.900–4.50** |

### What Drives Token Consumption

| Stage | Tokens consumed | Model |
|---|---|---|
| Query parsing (entity extraction) | ~500 input / ~100 output | gpt-4o-mini |
| Community map-reduce (per community) | ~2,000 input / ~300 output | gpt-4o-mini |
| Final reduce answer | ~30,000 input / ~3,000 output | gpt-4o / gpt-5 |

> The **final reduce step** accounts for ~95% of LLM cost.
> `MAX_CONTEXT_TOKENS` in `.env` directly controls this — lower it to cut costs.

---

## 8. Cost Optimisation Strategies

### Strategy 1 — Switch Final Answers to gpt-4o (Highest Impact)

**Savings: 85–95% of LLM cost**

```env
# .env
LLM_QUERY_MODEL=gpt-4o
LLM_QUERY_DEPLOYMENT=gpt-4o
```

Quality impact: Minimal for code Q&A. gpt-5's advantage is mathematical/abstract reasoning, not Java code understanding.

---

### Strategy 2 — Reduce Context Window

**Savings: 30–50% of LLM cost**

```env
MAX_CONTEXT_TOKENS=32000   # down from 64000
```

Cuts the context fed to the final LLM call in half. Slightly reduces answer completeness for very broad questions but dramatically cheaper.

---

### Strategy 3 — Azure Reserved Instances

**Savings: 35–45% on VM compute**

Buy a 1-year reserved instance for the Neo4j VM:

| VM | On-demand/mo | 1-yr Reserved/mo | Saving |
|---|---|---|---|
| Standard_E4s_v3 | $252 | $160 | $92/mo ($1,104/yr) |
| Standard_E8s_v3 | $504 | $319 | $185/mo ($2,220/yr) |

Only do this once you've validated the deployment is stable.

---

### Strategy 4 — Auto-Shutdown for Non-Production Hours

**Savings: 50–65% on VM compute**

If the team works 8am–8pm on weekdays only (~60 hours/week vs 168 hours):

```
Active hours: 60/168 = 36% of the time
Standard_E4s_v3: $252 × 0.36 = $91/mo (vs $252)
```

Configure via Azure Auto-shutdown in the VM blade. Neo4j data persists on the managed disk.

---

### Strategy 5 — Redis Semantic Query Cache

**Savings: 40–60% of LLM cost at scale**

Cache the full LLM response keyed by a hash of the retrieved context.
When 10 developers ask "how does token validation work", only the first query hits the LLM.

Currently Redis is used for session state — extending it to semantic caching requires a ~1-day implementation effort.

---

### Strategy 6 — Reduce Community Summarisation Frequency

**Savings: One-time ingestion cost**

Community summarisation (the most expensive ingestion step) re-runs on every `py main.py ingest`.
Disable it for incremental ingests of small repo changes:

```env
COMMUNITY_SUMMARIZATION_ENABLED=false
```

Re-enable only when adding new repos or after major code changes.

---

## 9. Recommended Starting Point

For the current WSO2 IS analysis use case (5–10 developers):

### Immediate Setup

```
VM:                Standard_E4s_v3  ($252/mo)
Disk:              Premium SSD P15 256GB  ($38/mo)
LLM_QUERY_MODEL:   gpt-4o  (not gpt-5)
MAX_CONTEXT_TOKENS: 32000
```

**Estimated monthly total: ~$420–500/mo**

### After 3 Months (if actively used)

- Buy 1-year reserved instance for the VM → saves ~$92/mo
- Enable Redis semantic cache → saves ~$100–150/mo in LLM costs
- **New estimated total: ~$250–350/mo**

---

## 10. vs. Commercial Alternatives

| Tool | What it does | Cost (10 devs) | Cost (100 devs) |
|---|---|---|---|
| **CodeNexus (gpt-4o)** | Deep architectural Q&A, cross-repo flow tracing | ~$500–650/mo | ~$2,400/mo |
| **CodeRabbit** | AI PR review, code suggestions | $190/mo | $1,900/mo |
| **GitHub Copilot** | Inline code completion | $190/mo | $1,900/mo |
| **Sourcegraph (self-hosted)** | Code search, symbol navigation | ~$200/mo infra | ~$500/mo infra |
| **Swimlane / LeanIX** | Architecture documentation (manual) | $3,000–10,000/mo | $10,000–50,000/mo |

### Key Insight

CodeNexus does **not compete** with CodeRabbit or Copilot — those are developer productivity tools.
It competes with **architecture documentation and code intelligence consultancies**.
The question is not "CodeNexus vs CodeRabbit" — it is "CodeNexus vs hiring a consultant to document your codebase."

A typical architecture audit of a system the size of WSO2 IS (multi-repo, 500k+ lines) costs **$50,000–200,000** as a one-time engagement.
CodeNexus provides a queryable, always-current version of that knowledge for ~$500/mo.

---

## Appendix — Azure VM Quick Reference

| VM SKU | vCPU | RAM | On-demand/mo | 1-yr Reserved/mo | Best for |
|---|---|---|---|---|---|
| Standard_B4ms | 4 | 16 GB | $140 | $89 | Dev/test only |
| Standard_D4s_v3 | 4 | 16 GB | $192 | $122 | App server (no Neo4j) |
| Standard_E4s_v3 | 4 | 32 GB | $252 | $160 | **Tier A/B (recommended)** |
| Standard_E8s_v3 | 8 | 64 GB | $504 | $319 | Tier C Neo4j node |
| Standard_E16s_v3 | 16 | 128 GB | $1,008 | $638 | Tier D Neo4j node |
| Standard_M32ms | 32 | 875 GB | $4,537 | $2,873 | Extreme scale only |

---

*Generated by CodeNexus project tooling. Prices as of March 2026 — verify current rates before procurement.*
