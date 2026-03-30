# RAG Is Dead — Part 1: Why Vector Search Fails for Code (And What to Do Instead)

*Part 1 of 4 in the "RAG Is Dead" series*

---

You have 10 million lines of Java. A developer joins the team and asks:

> "Is it safe to remove the `IMPERSONATED_SUBJECT` constant?"

You have a RAG system. You embed the entire codebase. You search with the query. You get back five method chunks that mention the word "impersonated." The LLM reads them and says: *"Based on the retrieved context, this constant appears to be used in token exchange flows. Removing it may cause issues."*

Useless. Vague. Could be wrong. The developer still has to go grep the entire codebase manually.

This is the dirty secret of RAG applied to code: **it retrieves text. Code is not text.**

---

## What RAG Was Built For

RAG — Retrieval-Augmented Generation — was designed for documents. PDFs, articles, support tickets, Slack threads. Text that means something in isolation. You chunk it, embed it, and when a question arrives you find the most semantically similar chunks and hand them to an LLM.

For documents, this works beautifully. A chunk from a legal policy document contains the answer you need. The surrounding context is nice to have, not required.

**Code is the opposite.** A method body in isolation is almost meaningless. What makes a method important is not what it says — it's what calls it, what it calls, what it implements, and what contracts it is bound to.

---

## Three Ways Flat Vector Search Fails for Code

### 1. The Call Chain Problem

Consider this question: *"What breaks if I change the signature of `TokenHandler.validate()`?"*

A vector search returns methods that are semantically similar to `TokenHandler.validate` — methods that also validate tokens, methods with similar Javadoc. But that is not the question. The question is: **who calls this method?**

The answer lives in the call graph. It does not live in any individual chunk.

```
TokenExchangeGrantHandler.issue()
    └─ OAuthTokenReqMessageContext.getProperty()
        └─ TokenHandler.validate()          ← you want to change this
            └─ JWTTokenGenerator.generate()
                └─ IdentityDatabaseUtil.getDBConnection()  ← database touch
```

A vector search for "TokenHandler validate" returns chunks near that method. It does not return `IdentityDatabaseUtil.getDBConnection` — because that class has nothing semantically similar to validation in its Javadoc. But it IS in the blast radius. Changing `validate()` without knowing this means you ship a bug.

**Flat RAG has no concept of blast radius. It has no edges.**

---

### 2. The Invisible Wiring Problem

Enterprise Java — especially OSGi-based systems like WSO2 Identity Server — relies heavily on dependency injection. A class declares a dependency with an annotation:

```java
@Reference
private TokenValidator validator;
```

At runtime, the OSGi framework injects the concrete implementation. Which one? You have to trace the `@Component(service={TokenValidator.class})` annotation on the implementing class.

This wiring is **not in the source text of either class**. There is no import, no `new`, no method call that connects them in the raw text. A vector search over method bodies will never surface this connection.

Without this edge in your retrieval, your call graph has silent gaps. You trace a flow from an API entry point, hit an injected interface, and fall off a cliff. The LLM says "I cannot determine the complete flow" — because the data you gave it is genuinely incomplete.

**Flat RAG retrieves what is written. OSGi wiring is not written — it is inferred.**

---

### 3. The Specification Obligation Problem

Here is the most subtle failure.

WSO2 Identity Server implements OAuth 2.0 Token Exchange (RFC 8693). This means its `TokenExchangeGrantHandler` must enforce specific rules — for example, that a `subject_token_type` parameter must be present and validated.

The Java source code has zero comments saying "this implements RFC 8693 §2.1". It just implements it. The method does what the RFC requires because the developer read the RFC once and wrote the code accordingly.

A developer now asks: *"Does our token exchange implementation validate the `subject_token_type` as required by RFC 8693?"*

Vector search will return methods that handle token exchange. But it has no way to connect those methods to the specific requirement in RFC 8693 §2.1. It cannot answer whether the implementation is *compliant* — because compliance is a relationship between code and a specification document, and that relationship does not exist in any chunk.

**Flat RAG knows what the code does. It does not know what the code is supposed to do.**

---

## What the Right Answer Looks Like

The right answer to "is it safe to remove `IMPERSONATED_SUBJECT`?" is not a vague warning. It looks like this:

```
VERDICT: NO — constant is actively read in 3 production call sites.

Property access:
  - Writers (graph): 1 method — OAuthTokenReqMessageContext.setImpersonatedSubject()
  - Readers (graph): 2 methods
  - Read-like patterns (grep): 3 additional .get(IMPERSONATED_SUBJECT) calls

Production callers to change:
  1. TokenExchangeGrantHandler.java:847
     → oAuthTokenReqDTO.get(IMPERSONATED_SUBJECT) — must be replaced with null check
  2. ImpersonationGrantHandler.java:312
     → messageContext.getProperty(IMPERSONATED_SUBJECT) — remove or guard
  3. IdentityUtil.java:203 (test harness boundary)

Minimal change set:
  - Update 2 production files, 1 test
  - The constant itself can be removed only after these callers are updated
```

This is not a better chunk. This is a different kind of retrieval entirely.

---

## The GraphRAG Alternative

The fundamental shift is this: **stop treating a codebase as a bag of text. Treat it as a graph.**

```
Java source
    │
    ├── Tree-sitter AST parser
    │       ↓
    │   Structured objects:
    │   classes, methods, calls, annotations, modifiers
    │       ↓
    │   ┌──────────────┐     ┌─────────────────────────┐
    │   │  Graph Store │     │    Vector Store          │
    │   │  (Neo4j)     │     │    (ChromaDB)            │
    │   │              │     │                          │
    │   │  CALLS edges │     │  Method body → 384-dim   │
    │   │  EXTENDS     │     │  Javadoc → 384-dim       │
    │   │  RESOLVES_TO │     │  Community summaries     │
    │   │  (OSGi)      │     │  Flow narratives         │
    │   │  IMPLEMENTS  │     │  Global architecture     │
    │   │  _SPEC (RFC) │     │                          │
    │   └──────────────┘     └─────────────────────────┘
    │            │                         │
    │            └────────┬────────────────┘
    │                     │
    │              Every query uses both
    │              Graph for structure
    │              Vectors for meaning
```

When a question arrives, the system does not do a single vector search. It:

1. **Classifies** the question — is this asking about a symbol, an entity, a concept, or the whole system?
2. **Routes** to the right retrieval strategy — grep + graph traversal for symbols, hybrid BM25 + cosine for concepts, direct document fetch for architecture overviews
3. **Expands** via the call graph — blast radius, callers, callees, community clusters
4. **Grounds** via specification edges — what RFC obligations apply to the code in scope?
5. **Synthesises** — one LLM call with real, structured evidence

The answer is no longer a guess. It is a verdict with citations.

---

## The Proof Is in the RAM

One concrete result that surprised us: this architecture turned out to be dramatically more efficient than live Neo4j queries.

By exporting the graph to SQLite and loading all CALL edges into in-memory Python dicts at startup, the live API went from requiring **36–64 GB of RAM** (Neo4j JVM heap, live graph queries) to **3.7 GB** — with no loss in answer quality. For 100 Java repos.

That is not a tweak. That is a rethink of what "retrieval" means for code.

---

## What's Next

In the next three articles, we will go deep on the four layers of this system:

- **Part 2 — Building the Graph:** How Tree-sitter, Neo4j, and ChromaDB are assembled into a queryable knowledge base — including OSGi resolution and RFC specification grounding
- **Part 3 — The Query Engine:** The 4-route router, hybrid BM25 + cosine search, RRF fusion, and the prompt templates that produce structured verdicts instead of guesses
- **Part 4 — Scaling It Down:** The engineering decisions that took the system from 64 GB to 3.7 GB — and the path to 1.5 GB with int8 vector quantization

The core claim of this series: for codebases, **structure is the answer, not semantics**. Vectors tell you what code *looks like*. Graphs tell you what code *does to other code*. You need both — but the graph is the foundation.

---

*Built on CodeNexus v2 — a GraphRAG system for Java monolith analysis.*
*Target codebase: WSO2 Identity Server (~10 million lines of Java).*
