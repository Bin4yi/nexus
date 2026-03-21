"""
vectorstore/chunker.py
AST-aware sliding window chunker — Sprint 1 upgrade.

Splits UIR LogicUnit objects into text chunks for ChromaDB embedding using
a research-grade sliding window algorithm:

  1. Every chunk is prefixed with ``[Package: …] [Class: …]`` to preserve
     global context even when a long method is split across multiple windows.
  2. Methods longer than ``settings.chunk_size`` tokens are split into
     overlapping windows of ``settings.chunk_overlap`` tokens each.
  3. Two chunk types are produced per method (or per window):
       - code_logic:  raw method body — "what the code does"
       - code_intent: Javadoc description — "what the developer intended"
         (only one intent chunk per method, not per window)
"""
from __future__ import annotations
import re
from dataclasses import dataclass

import tiktoken

from config.settings import settings
from parsers.uir import LogicUnit
from parsers.javadoc_parser import JavadocParser

_javadoc = JavadocParser()
# cl100k_base is used by most modern OpenAI + Nomic models
_ENCODER = tiktoken.get_encoding("cl100k_base")


@dataclass
class EmbeddingChunk:
    """A single text chunk ready for ChromaDB embedding."""
    chunk_id: str           # unique: "{geid}_{chunk_type}[_{window_idx}]"
    geid: str               # links back to Neo4j node
    fqn: str
    text: str               # the text to embed
    chunk_type: str         # "code_logic" | "code_intent"
    language: str = "java"
    file_path: str = ""
    start_line: int = 0
    end_line: int = 0
    repo_name: str = ""     # source repository — used for --repo scoped queries


def _count_tokens(text: str) -> int:
    return len(_ENCODER.encode(text))


def _build_context_prefix(fqn: str) -> str:
    """
    Build the class/package context prefix that is prepended to every chunk.

    Example: "org.wso2.identity.AuthzEndpoint.handleOAuthRequest(String)"
      → "[Package: org.wso2.identity] [Class: AuthzEndpoint] "

    This prefix preserves global context so that any single sliding-window
    chunk can be understood in isolation by the embedding model.
    """
    # Strip parameter list if present
    base = fqn.split("(")[0] if "(" in fqn else fqn
    parts = base.split(".")
    if len(parts) >= 2:
        class_name = parts[-2]          # second to last = class name
        package_name = ".".join(parts[:-2])  # everything before = package
        if package_name:
            return f"[Package: {package_name}] [Class: {class_name}] "
        return f"[Class: {class_name}] "
    return ""


def _sliding_window(text: str, chunk_size: int, overlap: int) -> list[str]:
    """
    Split *text* into overlapping token windows.

    Args:
        text:       Full text to split.
        chunk_size: Maximum tokens per window.
        overlap:    Token overlap between consecutive windows.

    Returns:
        List of text windows. If text fits in one window, returns [text].
    """
    tokens = _ENCODER.encode(text)
    if len(tokens) <= chunk_size:
        return [text]

    stride = chunk_size - overlap
    if stride <= 0:
        stride = max(1, chunk_size // 2)

    windows: list[str] = []
    start = 0
    while start < len(tokens):
        end = min(start + chunk_size, len(tokens))
        window_tokens = tokens[start:end]
        windows.append(_ENCODER.decode(window_tokens))
        if end == len(tokens):
            break
        start += stride

    return windows


_METHOD_CALL_RE = re.compile(r"\.(\w{3,})\s*\(")
_STRING_LITERAL_RE = re.compile(r'"([A-Za-z0-9_.:/\-]{4,40})"')
_FIELD_ACCESS_RE = re.compile(r"\.([A-Z_]{4,})\b")


def _synthesize_intent(lu: "LogicUnit") -> str:
    """
    Synthesize a rich intent string for methods that lack Javadoc by
    extracting semantic signals from the method body.

    Signals extracted (no LLM required):
      1. Method signature  — name + parameter types + return type
      2. Called methods    — what this method delegates to
                             (.validateToken, .getAccessToken, .persist)
      3. String constants  — domain terms from string literals
                             ("grant_type", "access_token", "Bearer")
      4. Constant refs     — ALL_CAPS field accesses
                             (.GRANT_TYPE_AUTHORIZATION_CODE)

    Example output:
        validateScope(OAuthTokenReqMessageContext tokReqMsgCtx): boolean
        calls: validateInternalScope, checkAllowedScopes, getRequestedScopes
        refs: "openid", "scope", OAUTH2_SCOPE_SEPARATOR
    """
    method_name = lu.fqn.split("(")[0].rsplit(".", 1)[-1] if lu.fqn else ""
    params = ", ".join(
        f"{p.type_name} {p.name}" for p in lu.parameters
    ) if lu.parameters else ""
    ret = lu.return_type or "void"
    signature = f"{method_name}({params}): {ret}"

    if not lu.body_text:
        return signature

    body = lu.body_text[:3000]

    # Extract called method names (deduplicated, exclude trivial getters/setters)
    _trivial = {"get", "set", "is", "has", "add", "put", "log", "equals", "toString"}
    called = list(dict.fromkeys(
        m for m in _METHOD_CALL_RE.findall(body)
        if m.lower() not in _trivial
    ))[:8]

    # Extract meaningful string literals (domain terms)
    strings = list(dict.fromkeys(_STRING_LITERAL_RE.findall(body)))[:6]

    # Extract ALL_CAPS constant references (e.g. GRANT_TYPE_AUTHORIZATION_CODE)
    constants = list(dict.fromkeys(_FIELD_ACCESS_RE.findall(body)))[:5]

    parts = [signature]
    if called:
        parts.append("calls: " + ", ".join(called))
    if strings:
        parts.append("refs: " + ", ".join(f'"{s}"' for s in strings))
    if constants:
        parts.append("consts: " + ", ".join(constants))

    return "\n".join(parts)


class UIRChunker:
    """
    Converts LogicUnit UIR objects into EmbeddingChunk objects using
    an AST-aware sliding window algorithm with global context prefixes.
    """

    def chunk_logic_unit(self, lu: LogicUnit) -> list[EmbeddingChunk]:
        """
        Produce chunks from a LogicUnit:

        - code_logic (1 per window): method body text, prefixed with class/package context.
          Long methods are split into overlapping windows.
        - code_intent (at most 1): formatted Javadoc intent (never windowed — intent is
          always short enough to fit in one chunk).

        Args:
            lu: A LogicUnit UIR object

        Returns:
            List of EmbeddingChunk objects
        """
        chunks: list[EmbeddingChunk] = []
        prefix = _build_context_prefix(lu.fqn)

        # ── code_logic chunks ─────────────────────────────────────────────────
        logic_body = self._build_logic_body(lu)
        if logic_body.strip():
            full_text = prefix + logic_body
            windows = _sliding_window(
                full_text,
                chunk_size=settings.chunk_size,
                overlap=settings.chunk_overlap,
            )
            for i, window_text in enumerate(windows):
                chunk_id = (
                    f"{lu.geid}_code_logic"
                    if len(windows) == 1
                    else f"{lu.geid}_code_logic_{i}"
                )
                chunks.append(EmbeddingChunk(
                    chunk_id=chunk_id,
                    geid=lu.geid,
                    fqn=lu.fqn,
                    text=window_text,
                    chunk_type="code_logic",
                    file_path=lu.file_path,
                    start_line=lu.start_line,
                    end_line=lu.end_line,
                ))

        # ── code_intent chunk (one per method, not per window) ────────────────
        if lu.docstring.strip():
            parsed = _javadoc.parse(lu.docstring)
            intent_text = _javadoc.format_for_embedding(parsed)
        else:
            intent_text = _synthesize_intent(lu)

        if intent_text.strip():
            # Prefix intent too — a semantic query should know which class
            # the intent belongs to
            chunks.append(EmbeddingChunk(
                chunk_id=f"{lu.geid}_code_intent",
                geid=lu.geid,
                fqn=lu.fqn,
                text=prefix + intent_text,
                chunk_type="code_intent",
                file_path=lu.file_path,
                start_line=lu.start_line,
                end_line=lu.end_line,
            ))

        return chunks

    def chunk_all(self, logic_units: list[LogicUnit], repo_name: str = "") -> list[EmbeddingChunk]:
        """
        Chunk all LogicUnits, tagging each chunk with repo_name.
        Enables per-repo filtering at query time via ChromaDB 'where' metadata.
        """
        result: list[EmbeddingChunk] = []
        for lu in logic_units:
            for chunk in self.chunk_logic_unit(lu):
                if repo_name:
                    chunk.repo_name = repo_name
                result.append(chunk)
        return result

    def _build_logic_body(self, lu: LogicUnit) -> str:
        """Build the method signature + body text for code_logic chunks."""
        params = ", ".join(
            f"{p.type_name} {p.name}" for p in lu.parameters
        )
        ret = lu.return_type or "void"
        # Extract just the method name from the FQN
        method_name = lu.fqn.split("(")[0].rsplit(".", 1)[-1] if lu.fqn else ""
        signature = f"{ret} {method_name}({params})"
        if lu.body_text:
            return f"{signature}\n{lu.body_text}"
        return signature
