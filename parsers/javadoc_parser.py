"""
parsers/javadoc_parser.py
Dedicated Javadoc comment parser — extracts structured data from
``/** ... */`` blocks.

Extracts: summary sentence, ``@param``, ``@return``, ``@throws``,
``@see`` tags.  The parsed summary populates ``LogicUnit.docstring`` which
is embedded into ChromaDB ``code_intent`` collection for semantic search.
"""
from __future__ import annotations
import re
from typing import Any


class JavadocParser:
    """
    Parses a Javadoc comment string into structured tag data.

    Handles:
        @param name description
        @return description
        @throws ExceptionType description
        @exception ExceptionType description
        @see reference
        @deprecated reason
        {@link ClassName#method}
        {@code expression}
    """

    # Regex patterns
    _TAG_RE = re.compile(r"@(\w+)\s*(.*?)(?=\s*@|\s*\*/|$)", re.DOTALL)
    _INLINE_TAG_RE = re.compile(r"\{@(?:link|code)\s+(.*?)\}", re.DOTALL)
    _STRIP_RE = re.compile(r"^\s*/\*\*|\s*\*/\s*$|^\s*\*\s?", re.MULTILINE)

    def parse(self, javadoc: str) -> dict[str, Any]:
        """
        Parse a raw Javadoc string into structured fields.

        Args:
            javadoc: Raw comment string starting with /** and ending with */

        Returns:
            Dict with keys:
                description (str)   — the main text before any tags
                params (dict)       — {param_name: description}
                return (str|None)   — @return text
                throws (list[str])  — @throws descriptions
                see (list[str])     — @see references
                deprecated (str)    — @deprecated reason or ""
                links (list[str])   — all {@link ...} targets
        """
        if not javadoc:
            return {}

        # Clean the raw javadoc to plain text
        clean = self._strip_comment_syntax(javadoc)

        result: dict[str, Any] = {
            "description": "",
            "params": {},
            "return": None,
            "throws": [],
            "see": [],
            "deprecated": None,
            "links": [],
        }

        # Extract inline {@link} and {@code} targets
        result["links"] = self._INLINE_TAG_RE.findall(clean)

        # Split: description is text before the first @tag
        first_tag = re.search(r"@\w+", clean)
        if first_tag:
            result["description"] = clean[: first_tag.start()].strip()
            tags_text = clean[first_tag.start():]
        else:
            result["description"] = clean.strip()
            return result

        # Parse each @tag block
        for match in self._TAG_RE.finditer(tags_text):
            tag = match.group(1).lower()
            body = match.group(2).strip()

            if tag == "param":
                # @param name description
                parts = body.split(None, 1)
                if parts:
                    name = parts[0]
                    desc = parts[1].strip() if len(parts) > 1 else ""
                    result["params"][name] = desc

            elif tag == "return":
                result["return"] = body

            elif tag in ("throws", "exception"):
                # @throws ExceptionType description
                parts = body.split(None, 1)
                exc_type = parts[0] if parts else body
                exc_desc = parts[1].strip() if len(parts) > 1 else ""
                result["throws"].append(f"{exc_type}: {exc_desc}" if exc_desc else exc_type)

            elif tag == "see":
                result["see"].append(body)

            elif tag == "deprecated":
                result["deprecated"] = body

        return result

    def _strip_comment_syntax(self, javadoc: str) -> str:
        """Remove /** ... */ markers and leading * from each line."""
        return self._STRIP_RE.sub("", javadoc).strip()

    def format_for_embedding(self, parsed: dict[str, Any]) -> str:
        """
        Produce a clean natural-language summary from parsed Javadoc,
        suitable for ChromaDB 'code_intent' embedding.

        Example output:
            Retrieves a user by their unique ID.
            Parameters: id (Long) — The unique identifier of the user.
            Returns: The matching User object.
            Throws: UserNotFoundException: if no user found with given ID.
        """
        parts = []
        if parsed.get("description"):
            parts.append(parsed["description"])
        if parsed.get("params"):
            param_lines = []
            for name, desc in parsed["params"].items():
                param_lines.append(f"{name} — {desc}" if desc else name)
            parts.append("Parameters: " + "; ".join(param_lines))
        if parsed.get("return"):
            parts.append(f"Returns: {parsed['return']}")
        if parsed.get("throws"):
            parts.append("Throws: " + "; ".join(parsed["throws"]))
        if parsed.get("deprecated"):
            parts.append(f"Deprecated: {parsed['deprecated']}")
        return "\n".join(parts)
