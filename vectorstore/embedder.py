"""
vectorstore/embedder.py
ChromaDB embedder — stores code_logic and code_intent chunks.

Sprint 1 upgrade: adds ``FastEmbedder`` which uses ``fastembed`` with
dynamic ONNX runtime provider selection (CUDAExecutionProvider when a
CUDA-capable GPU is present, CPUExecutionProvider otherwise).

The existing ``ChromaEmbedder`` (sentence-transformers) is retained for
environments without fastembed installed.  Set ``USE_FASTEMBED=true`` in
``.env`` to switch to the Nomic model.

chromadb is imported lazily inside __init__ to avoid the pydantic-v1 shim
crash on Python 3.14.
"""
from __future__ import annotations
import logging
from typing import Optional

from config.settings import settings
from vectorstore.chunker import EmbeddingChunk

logger = logging.getLogger(__name__)

# ChromaDB collection names
COLLECTION_CODE_LOGIC  = "code_logic"
COLLECTION_CODE_INTENT = "code_intent"


class ChromaEmbedder:
    """
    Manages two ChromaDB collections:
    - code_logic:  "what the code does" — embeds method body text
    - code_intent: "what the intent is" — embeds Javadoc description
    Both use HuggingFace all-MiniLM-L6-v2 for 384-dim semantic embeddings.
    """

    def __init__(self, client):
        import chromadb
        from chromadb.utils import embedding_functions
        self.client = client
        self._emb_fn = embedding_functions.SentenceTransformerEmbeddingFunction(
            model_name=settings.embedding_model
        )
        self._logic_col = client.get_or_create_collection(
            name=COLLECTION_CODE_LOGIC,
            embedding_function=self._emb_fn,
        )
        self._intent_col = client.get_or_create_collection(
            name=COLLECTION_CODE_INTENT,
            embedding_function=self._emb_fn,
        )

    def upsert_chunks(self, chunks: list[EmbeddingChunk]) -> None:
        """
        Upsert a list of EmbeddingChunks into the appropriate ChromaDB collection.
        Batches large lists to avoid memory pressure.

        Args:
            chunks: List of EmbeddingChunk objects from UIRChunker
        """
        logic_chunks  = [c for c in chunks if c.chunk_type == "code_logic"]
        intent_chunks = [c for c in chunks if c.chunk_type == "code_intent"]

        # Deduplicate within each list by chunk_id (keep last occurrence).
        # ChromaDB raises DuplicateIDError if the same ID appears twice in one
        # upsert call, which can happen when the same method is parsed from
        # multiple paths (e.g., symlinked files or re-processed modules).
        logic_chunks  = list({c.chunk_id: c for c in logic_chunks}.values())
        intent_chunks = list({c.chunk_id: c for c in intent_chunks}.values())

        if logic_chunks:
            self._batch_upsert(self._logic_col, logic_chunks)
            logger.info("Upserted %d code_logic chunks", len(logic_chunks))

        if intent_chunks:
            self._batch_upsert(self._intent_col, intent_chunks)
            logger.info("Upserted %d code_intent chunks", len(intent_chunks))

    def semantic_search(
        self,
        query: str,
        collection: str = "code_intent",
        n_results: int = 10,
        where: Optional[dict] = None,
    ) -> list[dict]:
        """
        Perform semantic similarity search across a collection.

        Args:
            query:      Natural language query string
            collection: "code_logic" or "code_intent"
            n_results:  Number of results to return
            where:      Optional ChromaDB metadata filter

        Returns:
            List of dicts with keys: geid, fqn, chunk_type, distance, text
        """
        col = self._logic_col if collection == "code_logic" else self._intent_col
        kwargs = {"query_texts": [query], "n_results": n_results}
        if where:
            kwargs["where"] = where

        results = col.query(**kwargs)
        output = []
        if results["ids"]:
            for i, chunk_id in enumerate(results["ids"][0]):
                meta = results["metadatas"][0][i]
                output.append({
                    "chunk_id":   chunk_id,
                    "geid":       meta.get("geid", ""),
                    "fqn":        meta.get("fqn", ""),
                    "chunk_type": meta.get("chunk_type", ""),
                    "distance":   results["distances"][0][i],
                    "text":       results["documents"][0][i],
                })
        return output

    def get_by_geid(self, geid: str, collection: str = "code_intent") -> Optional[dict]:
        """Retrieve a chunk by GEID from a specific collection."""
        col = self._logic_col if collection == "code_logic" else self._intent_col
        result = col.get(where={"geid": geid}, limit=1)
        if result["ids"]:
            return {
                "geid": geid,
                "text": result["documents"][0],
                "metadata": result["metadatas"][0],
            }
        return None

    # ── Private ───────────────────────────────────────────────────────────────

    def _batch_upsert(
        self, collection, chunks: list[EmbeddingChunk]
    ) -> None:
        """Upsert chunks in batches of ``settings.embedding_batch_size``.

        If ChromaDB drops the connection mid-transfer (WinError 10053 / connection
        aborted) the batch is split in half and retried recursively, down to a
        minimum of 1 chunk.  This handles the case where a repo has unusually
        large method bodies that push the HTTP payload over ChromaDB's buffer.
        """
        bs = settings.embedding_batch_size
        for i in range(0, len(chunks), bs):
            batch = chunks[i : i + bs]
            # Guard against duplicate IDs within this slice (shouldn't happen
            # after upsert_chunks deduplication, but protects direct callers).
            seen: dict[str, EmbeddingChunk] = {}
            for c in batch:
                seen[c.chunk_id] = c
            batch = list(seen.values())
            self._upsert_with_retry(collection, batch)

    def _upsert_with_retry(  # noqa: C901
        self, collection, batch: list[EmbeddingChunk], _attempt: int = 0
    ) -> None:
        """Upsert a single batch, splitting in half on connection errors."""
        import time
        try:
            collection.upsert(
                ids=[c.chunk_id for c in batch],
                documents=[c.text for c in batch],
                metadatas=[
                    {
                        "geid":       c.geid,
                        "fqn":        c.fqn,
                        "chunk_type": c.chunk_type,
                        "language":   c.language,
                        "file_path":  c.file_path,
                        "start_line": c.start_line,
                        "end_line":   c.end_line,
                        "repo_name":  c.repo_name,
                    }
                    for c in batch
                ],
            )
        except Exception as exc:
            # ConnectionAbortedError / ConnectionError → payload too large.
            # Split batch in half and retry each half (min batch size = 1).
            err_str = str(exc).lower()
            is_connection_err = any(
                kw in err_str for kw in ("connection aborted", "10053", "connectionerror",
                                         "connection reset", "remotedisconnected",
                                         "protocol error")
            )
            if is_connection_err and len(batch) > 1:
                half = len(batch) // 2
                wait = min(2 ** _attempt, 8)   # 1s, 2s, 4s, 8s cap
                logger.warning(
                    "ChromaDB connection aborted on batch of %d — splitting to %d+%d "
                    "(attempt %d, wait %ds)",
                    len(batch), half, len(batch) - half, _attempt + 1, wait,
                )
                time.sleep(wait)
                self._upsert_with_retry(collection, batch[:half], _attempt + 1)
                self._upsert_with_retry(collection, batch[half:], _attempt + 1)
            else:
                raise


# ── Sprint 1: GPU-accelerated FastEmbed embedder ──────────────────────────────

class FastEmbedder:
    """
    GPU-accelerated batch embedder using ``fastembed`` + ONNX Runtime.

    Dynamically selects ``CUDAExecutionProvider`` if available; otherwise
    falls back to ``CPUExecutionProvider``.  Uses the ``nomic-ai/nomic-embed-text-v1.5``
    model (768-dim, significantly stronger than MiniLM for code).

    Usage:
        embedder = FastEmbedder()
        vectors = embedder.batch_embed(["def foo(): ...", "class Bar: ..."])
    """

    def __init__(self):
        try:
            import onnxruntime as ort
        except ImportError:
            raise ImportError(
                "onnxruntime is required for FastEmbedder. "
                "Install with: pip install onnxruntime  (CPU) or "
                "pip install onnxruntime-gpu  (CUDA)"
            )
        try:
            from fastembed import TextEmbedding
        except ImportError:
            raise ImportError(
                "fastembed is required for FastEmbedder. "
                "Install with: pip install fastembed  (CPU) or "
                "pip install fastembed-gpu  (CUDA)"
            )

        available = ort.get_available_providers()
        use_gpu = "CUDAExecutionProvider" in available
        providers = (
            ["CUDAExecutionProvider", "CPUExecutionProvider"]
            if use_gpu
            else ["CPUExecutionProvider"]
        )

        logger.info(
            "FastEmbedder: ONNX providers=%s  model=%s",
            providers, settings.fastembed_model,
        )

        self._model = TextEmbedding(
            model_name=settings.fastembed_model,
            providers=providers,
        )
        self._use_gpu = use_gpu

    @property
    def uses_gpu(self) -> bool:
        return self._use_gpu

    def batch_embed(self, texts: list[str]) -> list[list[float]]:
        """
        Embed a batch of texts and return their vectors.

        Args:
            texts: List of plain-text strings to embed.

        Returns:
            List of float vectors (one per input text).
        """
        if not texts:
            return []
        return [vec.tolist() for vec in self._model.embed(texts)]

    def embed_single(self, text: str) -> list[float]:
        """Convenience wrapper for a single text."""
        return self.batch_embed([text])[0]

    def upsert_chunks_to_chroma(
        self,
        collection,
        chunks: list[EmbeddingChunk],
        batch_size: int = 100,
    ) -> None:
        """
        Embed chunks with fastembed and upsert pre-computed vectors to ChromaDB.

        ChromaDB accepts pre-computed ``embeddings=`` so the collection must be
        created WITHOUT an embedding function (pass ``embedding_function=None``
        or use ``get_or_create_collection`` without specifying one).

        Args:
            collection: ChromaDB collection created without an embedding function.
            chunks:     EmbeddingChunk objects to upsert.
            batch_size: Number of chunks per batch.
        """
        for i in range(0, len(chunks), batch_size):
            batch = chunks[i: i + batch_size]
            # Deduplicate
            seen: dict[str, EmbeddingChunk] = {}
            for c in batch:
                seen[c.chunk_id] = c
            batch = list(seen.values())

            texts = [c.text for c in batch]
            vectors = self.batch_embed(texts)

            collection.upsert(
                ids=[c.chunk_id for c in batch],
                embeddings=vectors,
                documents=texts,
                metadatas=[
                    {
                        "geid": c.geid,
                        "fqn": c.fqn,
                        "chunk_type": c.chunk_type,
                        "language": c.language,
                        "file_path": c.file_path,
                        "start_line": c.start_line,
                        "end_line": c.end_line,
                        "repo_name": c.repo_name,
                    }
                    for c in batch
                ],
            )
        logger.info("FastEmbedder: upserted %d chunks to ChromaDB", len(chunks))
