"""
reasoning/reduce_step.py
Reduce step — synthesises the final answer from community summaries + grounded evidence.

CRITICAL DESIGN:
  The prompt template adapts to the query intent:
  - Safety/removal queries ("can I remove X", "is it safe to delete Y")
    → YES/NO verdict prompt grounded in exact grep caller counts
  - Blast-radius / impact queries
    → Impact analysis prompt citing real class/method names
  - Code queries ("show me", "what does X look like", "give me the implementation")
    → Code display prompt — injects actual source into response
  - General/conceptual queries
    → Architectural overview prompt

Code snippets are fetched from the mirror by CodeFetcher and injected
directly into the prompt — the LLM presents real code, not invented code.
"""
from __future__ import annotations
import logging
import re

import tiktoken
from openai import OpenAI

from config.settings import settings, llm_chat
from reasoning.map_step import MapResult

logger = logging.getLogger(__name__)

_ENCODER = tiktoken.get_encoding("cl100k_base")

NO_COMMUNITIES_MESSAGE = "No relevant code communities were identified for this query."

_SAFETY_RE = re.compile(
    r'\b(is it safe|safely remove|safe to remove|safe to delete|ok to remove|okay to remove|'
    r'can i remove|can i delete|can i drop|should i remove|should i delete|should i drop|'
    r'is .+ safe to|still used|unused|dead code|'
    r'(?:remove|delete|drop|deprecate|eliminate)\b.{0,40}\bsafely\b)\b',
    re.IGNORECASE,
)
_CODE_RE = re.compile(
    r'\b(show me|show the|give me|display|print|what does .+ look like|'
    r'how is .+ implemented|implementation of|source of|code for|source code of|'
    r'how does .+ work|read the|view the|open the|'
    r'how to (introduce|implement|add|integrate|extend|support|enable|write|create)|'
    r'how do i (introduce|implement|add|integrate|extend|support|enable|write|create)|'
    r'what (files|classes|methods|code) (should|do) i (change|modify|edit|update|add)|'
    r'where (should|do) i (add|change|modify|implement))\b',
    re.IGNORECASE,
)
_IMPACT_KEYWORDS = frozenset([
    "blast radius", "what breaks", "what calls", "callers",
    "what changes", "impact", "dependency", "depends",
])

_CAPABILITY_RE = re.compile(
    # Only match true feature/multi-value capability questions — NOT simple enforcement checks
    # ("is X mandatory?" / "is X required?" are enforcement questions → use GENERAL, not CAPABILITY)
    r'\b(does this support|does it support|can it support|'
    r'is it possible to|is it possible for|can .+ be done|can i .+ without|'
    r'without .+ token|can .+ bypass|can .+ skip|'
    r'does .+ support multiple|can .+ handle multiple|'
    r'does .+ allow|is .+ allowed to|'
    r'does the (code|system|flow|handler|impl) (support|enforce|handle|allow)|'
    r'is .+ (supported|allowed|enforced|checked|validated|enabled))\b',
    re.IGNORECASE,
)

_NARRATIVE_RE = re.compile(
    r'\b(story|full story|full flow|end.to.end|start.to.end|e2e flow|'
    r'walk me through|walkthrough|walk through|trace through|'
    r'how does .+ work|how do .+ work|how is .+ implemented|how does .+ get |'
    r'how is .+ evaluated|how does .+ evaluate|how are .+ evaluated|'
    r'how does .+ run|how does .+ execute|how does .+ process|'
    r'how does .+ handle|how does .+ perform|'
    r'how is .+ (done|happen|happening|handled|performed|triggered|invoked|called|'
    r'checked|validated|processed|enforced|built|constructed|generated|issued|'
    r'validated|verified|resolved|determined|computed|calculated)|'
    r'how (does|do|did|is|are|was|were) .+ (work|happen|flow|run|execute|get processed|'
    r'get validated|get checked|get called|get triggered|get built|get issued)|'
    r'explain (the |this |how |what ).+(flow|process|validation|mechanism|logic|'
    r'handling|sequence|chain|pipeline|lifecycle|handshake|handoff|path)|'
    r'what happens (when|during|after|before|if)|'
    r'explain how|explain the flow|explain the process|explain the sequence|'
    r'from start|beginning to end|step by step|step-by-step|'
    r'full picture|overall flow|full journey|complete flow|'
    r'how this implements|how the .+ implements|how it implements|'
    r'overall architecture|overarching architecture|system architecture|'
    r'architecture overview|architectural overview|high.level architecture|'
    r'system overview|what is the architecture|describe the architecture)\b',
    re.IGNORECASE,
)


def detect_query_intent(query: str) -> str:
    """
    Standalone intent classifier — importable by main.py without instantiating ReduceStep.
    Returns one of: 'code', 'safety', 'capability', 'impact', 'narrative', 'general'.
    """
    q = query.lower()
    if _CODE_RE.search(q):
        return "code"
    if _SAFETY_RE.search(q):
        return "safety"
    if _CAPABILITY_RE.search(q):
        return "capability"
    if any(kw in q for kw in _IMPACT_KEYWORDS):
        return "impact"
    if _NARRATIVE_RE.search(q):
        return "narrative"
    return "general"

MAX_TOKENS     = settings.max_context_tokens   # hard ceiling (default 32k, gpt-4o-mini supports 128k)
SYSTEM_TOKENS  = 200
OUTPUT_RESERVE = 6000          # default for safety/capability/impact — room for a solid answer
NARRATIVE_RESERVE = 20000      # for story/walkthrough/explain-flow — gpt-5 burns ~8-12k on internal reasoning before writing
DATA_BUDGET    = MAX_TOKENS - SYSTEM_TOKENS - OUTPUT_RESERVE  # token budget for input data

# Code snippets get 65% of the input budget — the primary signal for a code assistant.
# Community summaries get 30% — architectural context.
# Evidence (grep list) gets the remaining 5%.
SNIPPET_BUDGET          = int(DATA_BUDGET * 0.65)
SUMMARY_BUDGET          = int(DATA_BUDGET * 0.30)
EVIDENCE_BUDGET         = DATA_BUDGET - SNIPPET_BUDGET - SUMMARY_BUDGET

# For narrative queries — same ratios, but output reserve is larger.
NARRATIVE_DATA_BUDGET    = MAX_TOKENS - SYSTEM_TOKENS - NARRATIVE_RESERVE
NARRATIVE_SUMMARY_BUDGET = int(NARRATIVE_DATA_BUDGET * 0.35)  # 35% summaries
NARRATIVE_SNIPPET_BUDGET = int(NARRATIVE_DATA_BUDGET * 0.60)  # 60% code (more code for story)

NO_COMMUNITIES_MESSAGE = "No relevant code communities were identified for this query."

# ── System prompts ─────────────────────────────────────────────────────────────

SYSTEM_SAFETY = (
    "You are a senior Java architect performing an exact safety assessment. "
    "You have been given EXACT grep evidence showing every location where a symbol is used. "
    "Your job is to give a direct YES or NO verdict on whether it is safe to remove/change the symbol. "
    "Base your verdict ONLY on the evidence provided — if grep shows zero production callers, say YES. "
    "DO NOT invent risks that are not supported by the evidence."
)

SYSTEM_IMPACT = (
    "You are a principal Java architect writing a formal code impact review. "
    "You have been provided with exact grep evidence AND impacted code community summaries. "
    "Your review MUST reference the specific file names and line numbers from the grep evidence. "
    "Do NOT use generic placeholders. Generate a concrete, actionable impact analysis."
)

SYSTEM_GLOBAL = (
    "You are a senior solutions architect synthesizing a global architecture overview "
    "from pre-computed community summaries of a WSO2 Identity Server codebase.\n\n"
    "You have been given hierarchical GraphRAG summaries (L1 community → L2 subsystem → "
    "L3 global). These summaries were generated from actual code analysis.\n\n"
    "YOUR JOB: Produce a clear, structured architecture overview answering the developer's "
    "question. Use the subsystem summaries as your primary evidence.\n\n"
    "STRUCTURE:\n"
    "1. **System Purpose** — What this system does and who uses it.\n"
    "2. **Core Subsystems** — The major architectural domains (auth, token management, "
    "persistence, etc.) and what each is responsible for.\n"
    "3. **Key Flows** — The most important data/request flows through the system.\n"
    "4. **Integration Points** — How subsystems connect to each other and to external systems.\n"
    "5. **Design Patterns** — Notable architectural patterns visible across the codebase.\n\n"
    "RULES:\n"
    "- Reference specific subsystem names and class names from the summaries.\n"
    "- Be concrete and specific — name actual classes, interfaces, and packages.\n"
    "- Do not invent behaviour beyond what the summaries describe.\n"
    "- This is an architecture overview — you MAY synthesize across subsystems."
)

TEMPLATE_GLOBAL = """
## Question
{query}

## Global Architecture Summary (L3 — synthesized from all communities)
{global_summary}

## Subsystem Summaries (L2 — domain-level breakdowns)
{subsystem_summaries}

---

Answer the question using the architectural summaries above.
Be specific: name subsystems, key classes, and how they relate.
""".strip()

SYSTEM_GENERAL = (
    "You are a code knowledge base for WSO2 Java repositories. "
    "Your job is to show what code ALREADY EXISTS that is relevant to the question — "
    "not to give instructions, not to write new code.\n\n"
    "ALWAYS show the actual code using ```java fenced blocks with file path and line numbers. "
    "Explain what the existing code does. "
    "If something is not in the evidence, say so and name the class/method that would contain it. "
    "NEVER speculate. NEVER invent code. NEVER give generic advice."
)

SYSTEM_NARRATIVE = (
    "You are a senior Java engineer explaining a codebase to a developer who needs to understand "
    "how a feature is implemented end-to-end. You have been given actual source code snippets, "
    "grep evidence, and architectural community summaries.\n\n"

    "YOUR JOB: Tell the complete story — from the entry point where the request arrives, "
    "through every layer of processing, to the final output or decision. "
    "Think of this as a guided tour through the code.\n\n"

    "MANDATORY STRUCTURE:\n"
    "1. **Entry Point** — Where does the feature begin? What class/method receives the initial "
    "request or trigger? What data arrives?\n"
    "2. **Parsing & Validation** — How is the input parsed, validated, or resolved? "
    "What classes do this work?\n"
    "3. **Core Logic** — What is the heart of the implementation? Walk through the key methods "
    "in call order. Name the classes and methods with their responsibilities.\n"
    "4. **Data Flow** — How does the data transform as it moves through each layer? "
    "What structures carry it (DTOs, tokens, maps)?\n"
    "5. **Integration Points** — What external systems, databases, or services are called? "
    "How does the code interact with them?\n"
    "6. **Output / Result** — What is produced at the end? How is the response built and returned?\n"
    "7. **Key Design Decisions** — Any architectural patterns, extension points, or notable "
    "design choices visible in the code.\n\n"

    "RULES:\n"
    "- Cite exact class names and method names from the evidence.\n"
    "- Quote short code lines (1-3 lines) to anchor the narrative in reality.\n"
    "- If a part of the flow is not visible in the evidence, say so explicitly.\n"
    "- NEVER invent behaviour. NEVER speculate about what the code \"should\" do.\n"
    "- NEVER give generic descriptions — every paragraph must reference specific code."
)

SYSTEM_CAPABILITY = (
    "You are a senior Java architect answering a capability question about a WSO2 codebase. "
    "You have been given ACTUAL SOURCE CODE read from the repository, grep evidence, "
    "and community summaries.\n\n"

    "YOUR JOB: Determine whether the system ACTUALLY implements the capability the "
    "developer is asking about. Not whether the data types could theoretically hold it — "
    "whether the running code actually does it.\n\n"

    "MANDATORY REASONING STEPS (do all of them):\n"
    "1. IDENTIFY the specific method(s) that handle the feature the question asks about.\n"
    "2. TRACE THE DATA FLOW: Where does the input originate? How is it parsed? "
    "What processing happens? What is the output structure?\n"
    "3. LOOK FOR ABSENCE: If the question asks 'does X support Y', and you see no code "
    "that implements Y (no parsing, no iteration, no recursive construction, no multi-value "
    "handling), that absence IS your answer — the capability is NOT supported.\n"
    "4. GIVE A VERDICT with specific code evidence.\n\n"

    "CRITICAL REASONING RULES:\n"
    "- TYPE ≠ BEHAVIOR: A parameter declared as List<String> does NOT prove multi-value "
    "support. You MUST find WHERE and HOW the list is actually populated. If the list "
    "always contains exactly one element, multi-value is NOT supported.\n"
    "- SAME NAME ≠ SAME THING: 'audience' in a JWT aud claim (inherently a list per JWT spec) "
    "is a DIFFERENT concept from 'audience' as an HTTP request parameter. Two things "
    "sharing a name does not make them the same feature. Always check what layer you are in.\n"
    "- CONSTRUCTION DEPTH: Collections.singletonMap(K, V) creates exactly ONE key-value pair. "
    "If the code builds nested structure via singletonMap, the nesting depth is exactly what "
    "you see — one level. That is NOT evidence of recursive/multi-level nesting like "
    "{a: {b: v, a: {b: v2}}}. Count the actual construction depth visible in the code.\n"
    "- FIXED CODE = FIXED BEHAVIOR: If a method always builds the same structure with "
    "no loops, no recursion, no dynamic depth calculation, then the output structure is "
    "fixed — it does not support variable depth.\n"
    "- ABSENCE IS EVIDENCE: If you see no split(), no delimiter parsing, no recursion, "
    "no multi-value iteration, no dynamic structure building — that is STRONG evidence "
    "the capability is NOT supported. Do NOT assume missing code exists elsewhere.\n\n"

    "OUTPUT RULES:\n"
    "- Cite exact class name, method name, and what the code does.\n"
    "- If the evidence shows the code does NOT implement the asked capability, say NO.\n"
    "- If the evidence shows the code DOES implement it, say YES with proof.\n"
    "- Say 'cannot determine' ONLY if the relevant method body is truly absent from all evidence.\n"
    "- NEVER give generic advice. NEVER speculate about what the code 'should' do."
)

SYSTEM_EXPLAIN = (
    "You are a senior Java engineer explaining a method or class to a developer who is "
    "debugging or onboarding. You have been given the ACTUAL source code. "
    "Your job: explain what this code does, what its contract is (inputs/outputs/side-effects), "
    "and what common failure modes exist. Cite line numbers. Be concise and precise. "
    "NEVER invent behaviour not visible in the code."
)

SYSTEM_IMPLEMENTATION = (
    "You are a code knowledge base. Your ONLY job is to surface what code ALREADY EXISTS "
    "in the repository that is relevant to the developer's question.\n\n"
    "WHAT TO DO:\n"
    "- Show every relevant code block verbatim using ```java fenced blocks.\n"
    "- For each block: state the file path, line numbers, and what it does.\n"
    "- Explain how the existing pieces connect to each other.\n"
    "- If something is NOT implemented yet in the evidence, say so explicitly.\n\n"
    "WHAT NOT TO DO:\n"
    "- DO NOT write new code that is not in the evidence.\n"
    "- DO NOT create 'Files to change' or 'New/modified code' sections.\n"
    "- DO NOT give step-by-step instructions.\n"
    "- DO NOT invent methods, classes, or logic not visible in the evidence.\n\n"
    "RULES:\n"
    "- Every claim must be backed by a code block from the evidence.\n"
    "- Cite exact file path and line number for every reference.\n"
    "- If the relevant code is absent from the evidence, say 'not found in indexed code'."
)

SYSTEM_DEBUG = (
    "You are a senior Java debugger performing root-cause analysis. "
    "You have been given: the exception/error, the stack trace frames, and the ACTUAL source "
    "code at each relevant location. "
    "Your job: identify the EXACT line that throws/causes the error, explain WHY it occurs "
    "(what precondition was violated), and give a concrete fix with a code diff. "
    "Cite exact file paths and line numbers. Do NOT give generic advice."
)

# ── Prompt templates ───────────────────────────────────────────────────────────

TEMPLATE_SAFETY = """
## Question
{query}

## Exact Grep Evidence (every occurrence of this symbol in the codebase)
{grep_evidence}

## Architectural Context (impacted community summaries)
{community_summaries}

---

Answer the question with a precise safety verdict. Use these rules:
- If **Production usages = 0**: Verdict is **YES** — safe to remove. Only clean up the definition file and imports.
- If **Production usages = N > 0**: Verdict is **YES WITH CHANGES** — can be removed after updating those N call sites.
  List exactly which files and methods must be updated.
- Never say NO unless removing the constant would cause a breaking API change visible to external consumers.
- Do NOT count the constant's own definition file or import statements as "callers".

Structure your answer:
1. **Verdict**: YES / YES WITH CHANGES / NO — one sentence.
2. **Production callers that must change** (from grep): List file + line + what to change at each call site.
   If none, say "No production callers — only the definition must be deleted."
3. **Test callers** (from grep): These are auto-updated when the constant is removed.
4. **Minimal change set**: The exact steps (1-3 lines) to safely remove this constant.
""".strip()

TEMPLATE_IMPACT = """
## Request
{query}

## Grep Evidence (exact occurrences in the codebase)
{grep_evidence}

## Directly Affected Methods (from graph traversal)
{primary_targets}

## Impacted Code Communities (blast radius)
{community_summaries}

---

Write a structured architectural impact review. Reference the actual Java class names and line numbers above.

1. **Direct Impact**: Which files/methods must change, with exact file paths.
2. **Blast Radius**: Which other systems are affected and why (cite actual class names from above).
3. **Top Risks**: The 3 most concrete risks.
4. **Change Plan**: What to do and in what order.
""".strip()

TEMPLATE_CODE = """
## Question
{query}

## Actual Source Code (read directly from repository mirror)
{code_snippets}

## Related Methods (from graph traversal)
{primary_targets}

## Architectural Context
{community_summaries}

---

Answer the question and include the relevant code blocks in your response.
When showing code, use ```java fenced blocks. Explain what the code does and
how it relates to the question. If modifications are suggested, show the
before/after diff.
""".strip()

TEMPLATE_IMPLEMENTATION = """
## Question
{query}

## Actual Source Code (read directly from repository mirror)
{code_snippets}

## Related Methods and Classes (from graph traversal)
{primary_targets}

## Architectural Context (community summaries)
{community_summaries}

---

Show the developer what ALREADY EXISTS in the codebase that is relevant to this question.

REQUIRED SECTIONS:
1. **What already exists** — show every relevant code block verbatim using ```java fenced blocks.
   Include file path and line numbers above each block. Show ALL relevant snippets from the evidence.
2. **How they connect** — explain which classes call which, and what each does. Cite file + line.
3. **What is NOT yet implemented** — if the question asks about a feature not visible in the evidence,
   state exactly which class/method would contain it and that it is not found in the indexed code.

RULES:
- Show code first, explain after. Use ```java blocks for every code reference.
- NEVER write new code. NEVER suggest changes. Only show what exists.
- If something is absent from the evidence, say "not found in indexed code — would likely be in <ClassName>".
""".strip()

TEMPLATE_CAPABILITY = """
## Question
{query}

## Code Evidence — Actual source code read from the repository
{code_snippets}

## Grep Evidence — Lines matching relevant terms in production code
{grep_evidence}

## Related Code Communities
{community_summaries}

---

Answer the capability question based ONLY on the Code Evidence and Grep Evidence above.

STEP 1 — Identify the relevant method(s):
  Which method(s) in the evidence directly handle the feature the question asks about?
  Name them explicitly.  If you cannot find the relevant method body, say so.

STEP 2 — Trace the data flow:
  For each relevant method, trace:
  a) WHERE does the input come from? (HTTP parameter, JWT claim, config, etc.)
  b) HOW is it parsed? (split on comma? iteration? single read?)
  c) WHAT structure is built? (singletonMap = 1 entry; HashMap with loop = dynamic)
  d) WHAT is the output? (single value, list, nested map?)

STEP 3 — Apply anti-patterns:
  - If a List<String> parameter exists but you cannot see multi-value population → NOT supported
  - If Collections.singletonMap is used → exactly ONE nesting level, NOT recursive
  - If no split()/loop/recursion processes multiple values → multi-value NOT supported
  - If the question asks about request parameter X but evidence only shows JWT claim X → different thing
  - If the question asks about depth N nesting but construction code is flat → NOT supported

STEP 4 — Verdict:
  End with a single-line bold verdict:
  **YES** — this is supported (cite the exact method + line that proves it)
  **NO** — this is not supported (cite what the code actually does instead)
  **CANNOT DETERMINE** — the relevant method body is absent from all evidence
""".strip()

TEMPLATE_NARRATIVE = """
## Question
{query}

## Actual Source Code (read directly from repository mirror)
{code_snippets}

## Grep Evidence — Lines matching key terms in production code
{grep_evidence}

## Code Community Summaries (architectural context)
{community_summaries}

## Known Entry Points / Key Classes
{primary_targets}

---

Tell the complete start-to-end story of how this feature is implemented in the code.
Use the structure specified in your system instructions (Entry Point → Parsing → Core Logic
→ Data Flow → Integration Points → Output → Key Design Decisions).

Ground every paragraph in specific class names and methods from the evidence above.
If part of the flow is not visible in the evidence, say explicitly what is missing.
""".strip()

TEMPLATE_GENERAL = """
## Question
{query}

## Code Evidence — Actual source code read from the repository (±10 lines around each grep hit)
{code_snippets}

## Grep Evidence — Exact line matches in production code
{grep_evidence}

## Known Code Entities (file paths and FQNs)
{primary_targets}

## Code Community Summaries
{community_summaries}

---

Show what code EXISTS in the repository that answers this question. This is a knowledge base — show code, don't give instructions.

**Structure:**
1. **Direct answer** — 1-2 sentences stating what the code shows.
2. **Relevant code** — paste every relevant code block verbatim using ```java fenced blocks with file path + line numbers.
   Show ALL snippets from the evidence that are relevant — don't skip files.
3. **What each piece does** — for each code block, explain its role. Cite class name, method, line.
4. **Connections** — how do the shown pieces relate to each other? Who calls who?

**Rules:**
- Code blocks first, explanation after. Never describe code without showing it.
- NEVER write new code. NEVER suggest changes. Only show what already exists.
- If the evidence shows a null-check + throw, state the field is MANDATORY (cite line).
- If something is not in the evidence, say "not found in indexed code".
""".strip()

TEMPLATE_EXPLAIN = """
## Method / Class to Explain
{fqn}

## Source Code (read directly from repository mirror)
{code_snippets}

## Callers — who calls this (up to 2 hops)
{callers}

## Callees — what this calls directly
{callees}

---

Explain this code to a developer who needs to understand or debug it.

Structure your answer in exactly these sections:
1. **What it does** — 2-3 sentences on purpose and responsibility.
2. **Inputs / Outputs / Side-effects** — parameters, return value, state mutations, exceptions thrown.
3. **Key logic** — cite the most important lines by line number.
4. **Called by** — list callers and why they call this.
5. **Common failure modes** — what can go wrong here, what null/edge-case triggers an exception.
   Base this ONLY on the code above — do not invent failure modes.
""".strip()

TEMPLATE_DEBUG = """
## Error
{error}

## Stack Trace Frames (most relevant)
{stack_frames}

## Source Code at Each Frame (read directly from repository mirror)
{code_snippets}

## Grep Evidence — throw sites for this exception
{grep_evidence}

---

Perform root-cause analysis and give a concrete fix.

Structure:
1. **Root cause** — the EXACT line that causes the error (cite file path + line number).
2. **Why it happens** — what precondition / null / state triggers it.
3. **Fix** — a concrete code change. Show before/after in ```java blocks.
4. **Verify** — what to check to confirm the fix is correct.

RULES:
- Only reference evidence above. Do not invent behaviour.
- If the stack trace frame is not in the evidence, say "frame not in mirror — cannot inspect".
""".strip()


class ReduceStep:
    """
    Synthesise the final answer from Map results and grounded evidence.
    Uses query-intent detection to choose the correct prompt template.
    """

    def __init__(self, llm_client: OpenAI | None = None):
        self.llm = llm_client or settings.make_llm_client()
        # For Azure: use deployment name (llm_query_deployment → llm_deployment → llm_model)
        # For OpenAI: use model name (llm_query_model → llm_model)
        if settings.llm_provider.lower() == "azure":
            self._query_model = (
                settings.llm_query_deployment
                or settings.llm_deployment
                or settings.llm_model
            )
        else:
            self._query_model = settings.llm_query_model or settings.llm_model

    def explain(
        self,
        fqn: str,
        code_snippets: list,
        callers: list[dict],
        callees: list[dict],
    ) -> str:
        """
        Explain what a method/class does, for the `explain` developer command.
        Produces a structured explanation grounded entirely in the actual source.
        """
        snippets_text = self._format_code_snippets(code_snippets)
        callers_text  = self._format_graph_nodes(callers)
        callees_text  = self._format_graph_nodes(callees)

        prompt = TEMPLATE_EXPLAIN.format(
            fqn=fqn,
            code_snippets=snippets_text or "(source not readable from mirror)",
            callers=callers_text or "(no inbound callers found in graph)",
            callees=callees_text or "(no outbound callees found in graph)",
        )
        return llm_chat(self.llm, self._query_model, SYSTEM_EXPLAIN, prompt, OUTPUT_RESERVE)

    def debug_error(
        self,
        error: str,
        stack_frames: str,
        code_snippets: list,
        grep_evidence: list[dict],
    ) -> str:
        """
        Root-cause analysis for the `debug` command.
        Grounds the LLM in actual source code at each stack frame.
        """
        snippets_text = self._format_code_snippets(code_snippets)
        grep_text     = self._format_grep_evidence(grep_evidence)

        prompt = TEMPLATE_DEBUG.format(
            error=error or "(no exception name provided)",
            stack_frames=stack_frames or "(no stack trace provided)",
            code_snippets=snippets_text or "(source not readable from mirror)",
            grep_evidence=grep_text or "(no throw sites found via grep)",
        )
        return llm_chat(self.llm, self._query_model, SYSTEM_DEBUG, prompt, OUTPUT_RESERVE)

    def run(
        self,
        map_results: list[MapResult],
        query: str = "",
        primary_targets: list[dict] | None = None,
        code_snippets: list | None = None,    # CodeSnippet objects from CodeFetcher
        route: str = "",
        on_token=None,        # optional callable(str) — streams final answer tokens to caller
    ) -> str:
        """
        Execute the reduce step with intent-aware prompting.

        Args:
            map_results:     Scored community summaries
            query:           Original user query
            primary_targets: Grounded evidence (grep hits, graph nodes, semantic hits)
            code_snippets:   Actual source code blocks from CodeFetcher
            route:           Router decision (global/global_l2 triggers architecture prompt)
        """
        # Global architecture queries use a dedicated synthesis prompt
        if route.startswith("global") and map_results:
            return self._run_global(map_results, query)

        targets    = primary_targets or []
        intent     = self._detect_intent(query)
        if intent == "safety":
            system_msg = SYSTEM_SAFETY
        elif intent == "code":
            system_msg = SYSTEM_IMPLEMENTATION
        elif intent == "impact":
            system_msg = SYSTEM_IMPACT
        elif intent == "capability":
            system_msg = SYSTEM_CAPABILITY
        elif intent == "narrative":
            system_msg = SYSTEM_NARRATIVE
        else:
            system_msg = SYSTEM_GENERAL

        # Separate grep evidence from graph/semantic hits
        grep_hits     = [t for t in targets if t.get("source") == "grep"]
        graph_nodes   = [t for t in targets if t.get("source") == "graph"]
        semantic_hits = [t for t in targets if t.get("source") == "semantic"]

        grep_text    = self._format_grep_evidence(grep_hits)
        targets_text = self._format_graph_nodes(graph_nodes + semantic_hits)
        summaries_text, n_included = self._build_summaries_text(
            map_results,
            budget=NARRATIVE_SUMMARY_BUDGET if intent == "narrative" else SUMMARY_BUDGET,
        )

        snippets_text = self._format_code_snippets(
            code_snippets or [],
            budget=NARRATIVE_SNIPPET_BUDGET if intent == "narrative" else SNIPPET_BUDGET,
        )
        logger.info(
            "Reduce: intent=%s, grep=%d, graph=%d, communities=%d, snippets=%d",
            intent, len(grep_hits), len(graph_nodes), n_included, len(code_snippets or []),
        )

        # Guard: if there is literally no evidence at all, bail early.
        if not summaries_text and not snippets_text and not grep_hits and not graph_nodes:
            return NO_COMMUNITIES_MESSAGE

        # Guard: safety/impact queries with no grep or code evidence are unreliable.
        # Community summaries alone cannot prove a constant is unused — refuse to guess.
        if intent in ("safety", "impact") and not grep_hits and not graph_nodes and not snippets_text:
            return (
                "I cannot give a safe removal verdict without grep evidence. "
                "Try rephrasing with the exact UPPER_SNAKE_CASE constant name (e.g. IMPERSONATING_ACTOR) "
                "so the router can search the codebase for actual usages."
            )

        # Build prompt based on intent
        if intent == "narrative":
            prompt = TEMPLATE_NARRATIVE.format(
                query=query or "(no query)",
                code_snippets=snippets_text or "(no source code fetched)",
                grep_evidence=grep_text or "(no grep evidence found)",
                primary_targets=targets_text or "(no specific code entities found)",
                community_summaries=summaries_text or "(no community data — relying on code snippets and grep evidence above)",
            )
        elif intent == "code":
            prompt = TEMPLATE_IMPLEMENTATION.format(
                query=query or "(no query)",
                code_snippets=snippets_text or "(no source code fetched from mirror)",
                primary_targets=targets_text or "(no specific code entities found)",
                community_summaries=summaries_text or "(no community data)",
            )
        elif intent == "safety":
            prompt = TEMPLATE_SAFETY.format(
                query=query or "(no query)",
                grep_evidence=grep_text,
                community_summaries=summaries_text or "(no community data)",
            )
        elif intent == "capability":
            prompt = TEMPLATE_CAPABILITY.format(
                query=query or "(no query)",
                code_snippets=snippets_text or "(no source code fetched)",
                grep_evidence=grep_text or "(no grep evidence — no relevant symbols found in codebase)",
                community_summaries=summaries_text or "(no community data)",
            )
        elif intent == "impact":
            prompt = TEMPLATE_IMPACT.format(
                query=query or "(no query)",
                grep_evidence=grep_text,
                primary_targets=targets_text,
                community_summaries=summaries_text or "(no community data)",
            )
        else:
            # General — include all evidence sources
            prompt = TEMPLATE_GENERAL.format(
                query=query or "(no query)",
                code_snippets=snippets_text or "(no source code fetched)",
                grep_evidence=grep_text or "(no grep evidence found)",
                primary_targets=targets_text or "(no specific code entities found)",
                community_summaries=summaries_text or "(no community data)",
            )

        # Narrative/code/general queries get full output space for comprehensive answers.
        output_tokens = NARRATIVE_RESERVE if intent in ("narrative", "code", "general") else OUTPUT_RESERVE

        try:
            review = llm_chat(self.llm, self._query_model, system_msg, prompt, output_tokens,
                              on_token=on_token)
        except Exception as e:
            logger.error("LLM call failed (model=%s): %s", self._query_model, e)
            return (
                f"[LLM error: {type(e).__name__} — model '{self._query_model}' may not be "
                f"available on this Azure endpoint. Set LLM_QUERY_DEPLOYMENT to a valid "
                f"deployment name in .env and restart.]\n\nError: {e}"
            )
        logger.info("Reduce complete — %d chars", len(review))
        return review

    def count_prompt_tokens(self, map_results: list[MapResult]) -> int:
        summaries_text, _ = self._build_summaries_text(map_results)
        prompt = TEMPLATE_GENERAL.format(
            query="test",
            code_snippets="",
            grep_evidence="",
            primary_targets="",
            community_summaries=summaries_text,
        )
        return len(_ENCODER.encode(SYSTEM_IMPACT + prompt))

    def _run_global(self, map_results: list[MapResult], query: str) -> str:
        """Dedicated path for global/architecture overview queries."""
        # Separate L3 global doc from L2 subsystem summaries
        global_parts = []
        subsystem_parts = []
        for mr in map_results:
            meta = {}
            # MapResult doesn't carry metadata directly — use summary text heuristics
            text = mr.summary_text or ""
            if mr.community_id == -1 and "Global Architecture" in text[:200]:
                global_parts.append(text)
            elif mr.community_id == -1:
                subsystem_parts.append(text)
            else:
                subsystem_parts.append(text)

        global_summary = "\n\n".join(global_parts) if global_parts else "(no L3 global summary available)"
        subsystem_summaries = "\n\n---\n\n".join(subsystem_parts[:8]) if subsystem_parts else "(no L2 subsystem summaries available)"

        prompt = TEMPLATE_GLOBAL.format(
            query=query or "(architecture overview)",
            global_summary=global_summary,
            subsystem_summaries=subsystem_summaries,
        )

        answer = llm_chat(self.llm, self._query_model, SYSTEM_GLOBAL, prompt, NARRATIVE_RESERVE)
        logger.info("Global reduce complete — %d chars", len(answer))
        return answer

    # ── Private ────────────────────────────────────────────────────────────────

    def _detect_intent(self, query: str) -> str:
        """Delegate to module-level detect_query_intent."""
        return detect_query_intent(query)

    def _format_code_snippets(self, snippets: list, budget: int = SNIPPET_BUDGET) -> str:
        """Format CodeSnippet objects for the LLM prompt, staying within the token budget."""
        if not snippets:
            return "(no code snippets fetched)"
        parts = []
        used_tokens = 0
        for s in snippets:
            note = f"  ({s.context_note})" if s.context_note else ""
            block = (
                f"### `{s.fqn}`{note}\n"
                f"File: `{s.file_path}` (lines {s.start_line}\u2013{s.end_line})\n"
                f"```{s.language}\n{s.code}\n```"
            )
            block_tokens = len(_ENCODER.encode(block))
            if used_tokens + block_tokens > budget:
                # Include a truncated version if we haven't added anything yet
                if not parts:
                    parts.append(block)  # always include at least one snippet
                break
            parts.append(block)
            used_tokens += block_tokens
        return "\n\n".join(parts)

    def _format_grep_evidence(self, grep_hits: list[dict]) -> str:
        """
        Format grep hits with precise categorisation:
          - DEFINITION: the constant's own declaration (public static final String X = ...)
          - IMPORT: static import lines (automatically go away when constant is removed)
          - PRODUCTION USAGE: actual method-body usage in src/main/java
          - TEST USAGE: src/test/java usage (does not affect production safety)
        This is the critical data the LLM uses for its YES/NO safety verdict.
        """
        if not grep_hits:
            return "(no grep evidence — symbol not found in codebase)"

        definitions = []
        imports     = []
        prod_usage  = []
        test_usage  = []

        for h in grep_hits:
            path = h.get("file_path", "")
            text = h.get("text", "")
            edge = h.get("edge_type", "")

            is_test = "src/test" in path or "Test.java" in path
            is_import = text.strip().startswith("import ")
            is_definition = ("static final" in text or "public static" in text) and "=" in text

            if is_definition and not is_test:
                definitions.append(h)
            elif is_import:
                imports.append(h)
            elif is_test:
                test_usage.append(h)
            else:
                prod_usage.append(h)

        lines = []

        if definitions:
            lines.append(f"**DEFINITION ({len(definitions)} location(s)):**")
            for h in definitions:
                lines.append(f"  - `{h['file_path']}` line {h.get('line_number', 'unknown')}")
                if h.get("text"):
                    lines.append(f"    ```java\n    {h['text'][:120]}\n    ```")

        if imports:
            lines.append(f"\n**IMPORT STATEMENTS ({len(imports)}) — these disappear automatically when constant is removed:**")
            for h in imports:
                lines.append(f"  - `{h['file_path']}`")

        if prod_usage:
            lines.append(f"\n**PRODUCTION USAGE ({len(prod_usage)} occurrence(s) — these WILL BREAK if constant is removed):**")
            for h in prod_usage:
                lines.append(f"  - `{h['file_path']}` — {h.get('edge_type', '')}")
                if h.get("text"):
                    lines.append(f"    ```java\n    {h['text'][:120]}\n    ```")
        else:
            lines.append("\n**PRODUCTION USAGE: NONE — No production method-body callers found.**")

        if test_usage:
            lines.append(f"\n**TEST USAGE ({len(test_usage)} occurrence(s) — does NOT affect safety):**")
            for h in test_usage:
                lines.append(f"  - `{h['file_path']}`")

        # Summary line — this is what the LLM should base its verdict on
        lines.append(
            f"\n**CALLER SUMMARY**: Definition: {len(definitions)}, "
            f"Imports (auto-removed): {len(imports)}, "
            f"Production usages: {len(prod_usage)}, "
            f"Test usages: {len(test_usage)}."
        )
        return "\n".join(lines)

    def _format_graph_nodes(self, nodes: list[dict]) -> str:
        if not nodes:
            return "(none found)"
        lines = []
        for t in nodes[:15]:
            fqn  = t.get("fqn", "")
            fp   = t.get("file_path", "")
            if "mirror" in fp:
                fp = fp[fp.find("mirror"):]
            edge = t.get("edge_type", "")
            lines.append(f"- `{fqn}`  ({fp}){f'  via {edge}' if edge else ''}")
        return "\n".join(lines)

    def _build_summaries_text(
        self, map_results: list[MapResult], budget: int = SUMMARY_BUDGET
    ) -> tuple[str, int]:
        if not map_results:
            return "", 0
        # For deterministic routes, all results have score=100; for semantic, filter >= 50
        high = [r for r in map_results if r.score >= 50]
        if not high:
            high = map_results[:10]  # fallback: take top 10 by whatever score they have
        lines = []
        for r in high:
            lines.append(f"### Community {r.community_id} (score {r.score}/100)\n{r.summary_text}\n")
        included = len(lines)
        # Trim from the bottom until within token budget, but ALWAYS keep at least 1 summary.
        while len(lines) > 1:
            if len(_ENCODER.encode("\n".join(lines))) <= budget:
                break
            lines.pop()
            included -= 1
        return "\n".join(lines), included
