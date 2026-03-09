"""
pipeline/ingest.py
High-speed multiprocessing ingestion pipeline — Sprint 1.

Uses ``concurrent.futures.ProcessPoolExecutor`` to parallelize Java file
parsing and chunking across all available CPU cores, dramatically reducing
wall-clock ingestion time for large Java monorepos (1,000+ files).

Architecture:
    - Each worker process gets a batch of .java file paths (serializable)
    - Workers run tree-sitter parsing independently (no shared state)
    - Results are returned as serialized dicts and reassembled in the main process
    - Chunking is also parallelized since it's CPU-bound (tokenisation)
    - Neo4j and ChromaDB I/O happen on the main process (connection pooling)

Entry point:
    ``python main.py ingest`` → calls ``run_parallel_ingest()``
"""
from __future__ import annotations
import logging
import os
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import asdict
from pathlib import Path
from typing import Optional

from config.settings import settings

logger = logging.getLogger(__name__)

# Number of files per worker batch — tune based on available RAM
_FILE_BATCH_SIZE = settings.parser_file_batch_size


# ── Worker function (runs in subprocess) ──────────────────────────────────────

def _parse_file_batch(args: tuple) -> dict:
    """
    Worker function: parse a batch of .java files and return chunked results.

    This function runs inside a subprocess so must re-import everything.
    Returns a plain dict (serializable via pickle) containing component data.

    Args:
        args: (file_paths: list[str], repo_name: str)

    Returns:
        Dict with keys:
          - "components": list of dicts (serialized Component data)
          - "chunks": list of dicts (serialized EmbeddingChunk data)
          - "errors": list of (file_path, error_msg) pairs
    """
    file_paths, repo_name = args

    # Imports inside worker to avoid multiprocessing spawn issues on Windows
    from parsers.java_parser import JavaParser
    from vectorstore.chunker import UIRChunker
    from dataclasses import asdict as dc_asdict

    parser = JavaParser()
    chunker = UIRChunker()

    result_components = []
    result_chunks = []
    errors = []

    for fp_str in file_paths:
        fp = Path(fp_str)
        try:
            components = parser.parse_file(fp, repo_name)
            for comp in components:
                # Serialize component for cross-process transfer
                comp_dict = _serialize_component(comp)
                result_components.append(comp_dict)

                # Chunk the logic units
                for lu in comp.logic_units:
                    for chunk in chunker.chunk_logic_unit(lu):
                        chunk.repo_name = repo_name
                        result_chunks.append(dc_asdict(chunk))
        except Exception as e:
            errors.append((fp_str, str(e)))

    return {
        "components": result_components,
        "chunks": result_chunks,
        "errors": errors,
    }


def _serialize_component(comp) -> dict:
    """Serialize a Component UIR object to a plain dict for pickle transfer."""
    return {
        "geid": comp.geid,
        "fqn": comp.fqn,
        "kind": comp.kind,
        "file_path": comp.file_path,
        "start_line": comp.start_line,
        "end_line": comp.end_line,
        "docstring": comp.docstring,
        "annotations": comp.annotations,
        "is_event_handler": comp.is_event_handler,
        "visibility": comp.visibility,
        "is_abstract": comp.is_abstract,
        "is_final": comp.is_final,
        "implements": comp.implements,
        "extends": comp.extends,
        "logic_units": [_serialize_lu(lu) for lu in comp.logic_units],
        "fields": [_serialize_field(f) for f in comp.fields],
    }


def _serialize_lu(lu) -> dict:
    """Serialize a LogicUnit UIR object to a plain dict."""
    return {
        "geid": lu.geid,
        "fqn": lu.fqn,
        "kind": lu.kind,
        "return_type": lu.return_type,
        "file_path": lu.file_path,
        "start_line": lu.start_line,
        "end_line": lu.end_line,
        "docstring": lu.docstring,
        "annotations": lu.annotations,
        "calls": lu.calls,
        "throws": lu.throws,
        "instantiates": lu.instantiates,
        "overrides": lu.overrides,
        "deprecated": lu.deprecated,
        "body_text": lu.body_text,
        "visibility": lu.visibility,
        "is_static": lu.is_static,
        "is_abstract": lu.is_abstract,
        "is_final": lu.is_final,
        "is_synchronized": lu.is_synchronized,
        "lifecycle_role": lu.lifecycle_role,
        "parameters": [
            {"name": p.name, "type_name": p.type_name, "annotations": p.annotations}
            for p in lu.parameters
        ],
    }


def _serialize_field(f) -> dict:
    return {
        "name": f.name,
        "type_name": f.type_name,
        "annotations": f.annotations,
        "is_injected": f.is_injected,
    }


# ── Main orchestration function ───────────────────────────────────────────────

def run_parallel_parse(
    java_files: list[Path],
    repo_name: str,
    max_workers: Optional[int] = None,
) -> tuple[list[dict], list[dict], list[tuple]]:
    """
    Parse and chunk a list of .java files using a process pool.

    Parallelizes across CPU cores using ``ProcessPoolExecutor``.
    Each worker handles a batch of ``_FILE_BATCH_SIZE`` files.

    Args:
        java_files:  List of .java file paths to parse.
        repo_name:   Repository name for GEID generation.
        max_workers: CPU cores to use (defaults to os.cpu_count()).

    Returns:
        Tuple of:
          - component_dicts: list of serialized Component dicts
          - chunk_dicts:     list of serialized EmbeddingChunk dicts
          - errors:          list of (file_path, error_message) pairs
    """
    if not java_files:
        return [], [], []

    workers = max_workers or os.cpu_count() or 4
    logger.info(
        "Parallel parse: %d files, %d workers, batch_size=%d",
        len(java_files), workers, _FILE_BATCH_SIZE,
    )

    # Split files into batches.
    # On Windows, prefix paths >260 chars with \\?\ to bypass MAX_PATH limit.
    import sys as _sys
    def _path_str(p: Path) -> str:
        s = str(p)
        if _sys.platform == "win32" and len(s) > 260 and not s.startswith("\\\\?\\"):
            return "\\\\?\\" + s
        return s

    batches = [
        ([_path_str(f) for f in java_files[i: i + _FILE_BATCH_SIZE]], repo_name)
        for i in range(0, len(java_files), _FILE_BATCH_SIZE)
    ]

    all_components: list[dict] = []
    all_chunks: list[dict] = []
    all_errors: list[tuple] = []

    # ProcessPoolExecutor — each batch runs in a separate subprocess
    with ProcessPoolExecutor(max_workers=workers) as executor:
        future_to_batch = {
            executor.submit(_parse_file_batch, batch): batch
            for batch in batches
        }
        completed = 0
        for future in as_completed(future_to_batch):
            completed += 1
            try:
                result = future.result()
                all_components.extend(result["components"])
                all_chunks.extend(result["chunks"])
                all_errors.extend(result["errors"])
            except Exception as e:
                logger.error("Worker batch failed: %s", e)
            if completed % 5 == 0 or completed == len(batches):
                logger.info(
                    "Parsed %d/%d batches — %d components so far",
                    completed, len(batches), len(all_components),
                )

    if all_errors:
        logger.warning(
            "%d files failed to parse (see debug log for details)", len(all_errors),
        )
        for fp, err in all_errors[:5]:
            logger.debug("Parse error %s: %s", fp, err)

    logger.info(
        "Parallel parse complete: %d components, %d chunks, %d errors",
        len(all_components), len(all_chunks), len(all_errors),
    )
    return all_components, all_chunks, all_errors
