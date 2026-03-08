"""
parsers/geid.py
Global Entity ID generator — deterministic SHA-256 hash linking Neo4j
nodes to ChromaDB vectors.

Formula: ``SHA256(f"{repo_name}::{fqn}")[:16]``.
The same GEID appears as ``n.geid`` in Neo4j and as ``metadata.geid`` in
ChromaDB, enabling bidirectional graph ↔ vector lookups.
"""
import hashlib


def generate_geid(repo_name: str, fqn: str) -> str:
    """
    Generate a 16-character deterministic GEID from repo name + FQN.

    Properties:
    - Deterministic: same inputs always produce the same output
    - Unique: SHA-256 collision probability is negligible (2^64 namespace)
    - Fixed-length: always 16 chars regardless of FQN length
    - Safe: hex chars only — compatible with all DB key constraints

    Args:
        repo_name: Short repository name (e.g. "user-service")
        fqn: Fully qualified name (e.g. "com.example.auth.UserService.getUser")

    Returns:
        16-character hex string e.g. "e7d2c8a1f3b50942"

    Example:
        >>> generate_geid("user-service", "com.example.auth.UserService.getUser")
        'e7d2c8a1f3b50942'
    """
    raw = f"{repo_name}::{fqn}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
