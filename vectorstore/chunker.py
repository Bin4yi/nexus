"""
vectorstore/chunker.py
Splits UIR LogicUnit objects into text chunks for ChromaDB embedding.
Produces two chunk types per method:
  - code_logic:  raw method body — "what the code does"
  - code_intent: Javadoc description — "what the developer intended"
"""
from __future__ import annotations
from dataclasses import dataclass

from parsers.uir import LogicUnit
from parsers.javadoc_parser import JavadocParser

_javadoc = JavadocParser()


@dataclass
class EmbeddingChunk:
    """A single text chunk ready for ChromaDB embedding."""
    chunk_id: str           # unique: "{geid}_{chunk_type}"
    geid: str               # links back to Neo4j node
    fqn: str
    text: str               # the text to embed
    chunk_type: str         # "code_logic" | "code_intent"
    language: str = "java"
    file_path: str = ""
    start_line: int = 0
    end_line: int = 0
    repo_name: str = ""     # source repository — used for --repo scoped queries


class UIRChunker:
    """
    Converts LogicUnit UIR objects into EmbeddingChunk objects.
    """

    def chunk_logic_unit(self, lu: LogicUnit) -> list[EmbeddingChunk]:
        """
        Produce up to 2 chunks from a LogicUnit:
        - code_logic (always): method body text
        - code_intent (if docstring exists): formatted Javadoc intent

        Args:
            lu: A LogicUnit UIR object

        Returns:
            List of EmbeddingChunk objects (1 or 2)
        """
        chunks = []

        # Chunk 1: code_logic — the raw implementation
        logic_text = self._build_logic_text(lu)
        if logic_text.strip():
            chunks.append(EmbeddingChunk(
                chunk_id=f"{lu.geid}_code_logic",
                geid=lu.geid,
                fqn=lu.fqn,
                text=logic_text,
                chunk_type="code_logic",
                file_path=lu.file_path,
                start_line=lu.start_line,
                end_line=lu.end_line,
            ))

        # Chunk 2: code_intent — the developer's description
        if lu.docstring.strip():
            parsed = _javadoc.parse(lu.docstring)
            intent_text = _javadoc.format_for_embedding(parsed)
            if intent_text.strip():
                chunks.append(EmbeddingChunk(
                    chunk_id=f"{lu.geid}_code_intent",
                    geid=lu.geid,
                    fqn=lu.fqn,
                    text=intent_text,
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

    def _build_logic_text(self, lu: LogicUnit) -> str:
        """Build a code logic text combining signature + body."""
        params = ", ".join(
            f"{p.type_name} {p.name}" for p in lu.parameters
        )
        ret = lu.return_type or "void"
        signature = f"{ret} {lu.fqn.split('.')[-1]}({params})"
        if lu.body_text:
            return f"{signature}\n{lu.body_text}"
        return signature
