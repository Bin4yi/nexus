"""
community/prompt_builder.py
Builds LLM prompts from community node data.
Enforces the strict 8,000-token context budget.
"""
from __future__ import annotations
import tiktoken

from config.settings import settings

# Token budget allocation
SYSTEM_TOKENS = 200
OUTPUT_RESERVE = 1000
DATA_BUDGET = settings.max_context_tokens - SYSTEM_TOKENS - OUTPUT_RESERVE  # 6800

_ENCODER = tiktoken.get_encoding("cl100k_base")  # GPT-4 compatible


def count_tokens(text: str) -> int:
    return len(_ENCODER.encode(text))


SYSTEM_PROMPT = (
    "You are a senior Java architect analyzing an enterprise codebase. "
    "You receive a cluster of related Java classes and methods that were "
    "grouped together by a graph community detection algorithm based on "
    "execution-flow edges (CALLS, INJECTS, IMPLEMENTS, EXTENDS, DEPENDS_ON). "
    "Your task is to produce a concise, technical summary of what this "
    "community of code does architecturally. Focus on the *class-level* "
    "responsibility — what the classes represent and how they collaborate — "
    "rather than listing individual method names."
)


def build_community_prompt(
    community_id: int,
    nodes: list[dict],
    boundary_edges: list[dict] | None = None,
) -> tuple[str, bool]:
    """
    Build the LLM prompt for summarizing one community.
    Truncates the node list if it would exceed the data budget.

    Args:
        community_id:   The community integer ID
        nodes:          List of node dicts from GDSClient.get_nodes_by_community()
        boundary_edges: Optional inter-community edge summary from
                        GDSClient.get_community_boundary_edges()

    Returns:
        Tuple of (prompt_text, was_truncated)
    """
    # Separate Component-level (classes/interfaces) from LogicUnit (methods)
    class_nodes  = [n for n in nodes if n.get("label") == "Component"]
    method_nodes = [n for n in nodes if n.get("label") != "Component"]

    header = (
        f"## Community #{community_id} — "
        f"{len(class_nodes)} classes, {len(method_nodes)} methods\n\n"
        "Analyze the following Java entities that form a cohesive cluster:\n\n"
    )

    node_lines = []

    # Classes first — architectural anchors
    if class_nodes:
        node_lines.append("### Classes / Interfaces")
        for node in class_nodes:
            fqn = node.get("fqn", "")
            kind = node.get("kind", "class")
            doc = node.get("docstring", "").strip()
            line = f"- [{kind}] {fqn}"
            if doc:
                doc_short = doc[:250] + "..." if len(doc) > 250 else doc
                line += f"\n  Doc: {doc_short}"
            node_lines.append(line)
        node_lines.append("")

    # Methods second — implementation detail
    if method_nodes:
        node_lines.append("### Methods")
        for node in method_nodes:
            fqn = node.get("fqn", "")
            kind = node.get("kind", "")
            doc = node.get("docstring", "").strip()
            line = f"- [{kind}] {fqn}"
            if doc:
                doc_short = doc[:200] + "..." if len(doc) > 200 else doc
                line += f"\n  Doc: {doc_short}"
            node_lines.append(line)

    # Boundary edges — how this community connects to others
    boundary_section = ""
    if boundary_edges:
        boundary_lines = ["\n### Connections to other communities"]
        for edge in boundary_edges[:6]:
            boundary_lines.append(
                f"- {edge['edge_type']} → community #{edge['target_community']} "
                f"({edge['cnt']} edges)"
            )
        boundary_section = "\n".join(boundary_lines)

    footer = (
        "\n\nIn 2-4 sentences, summarize:\n"
        "1. What architectural concern this community addresses\n"
        "2. The main responsibilities of these classes/methods\n"
        "3. Any notable design patterns or cross-cutting concerns\n"
    )

    truncated = False
    # Fit node lines within budget — include SYSTEM_PROMPT tokens to stay under hard limit
    output_reserve = OUTPUT_RESERVE
    while node_lines:
        body = header + "\n".join(node_lines) + boundary_section + footer
        if count_tokens(SYSTEM_PROMPT + body) <= (settings.max_context_tokens - output_reserve):
            break
        node_lines.pop()   # remove lowest-priority (last method) node
        truncated = True
        if not node_lines:
            break  # guard: header alone may exceed budget

    prompt = header + "\n".join(node_lines) + boundary_section + footer
    return prompt, truncated
