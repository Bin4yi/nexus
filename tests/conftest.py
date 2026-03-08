"""
tests/conftest.py
Shared pytest fixtures — Neo4j driver, ChromaDB client, sample UIR objects.
All integration fixtures require the Docker services to be running.

Imports for neo4j, chromadb, and redis are deliberately deferred inside
the fixtures so that unit tests collect and run on Python 3.14 without
requiring those packages (which carry pydantic-v1 shims incompatible with
Python 3.14) to be importable at collection time.
"""
import pytest
from pathlib import Path

from config.settings import settings
from parsers.uir import Project, Module, Component, LogicUnit, Parameter


# ── Infrastructure fixtures ───────────────────────────────────────────────────

@pytest.fixture(scope="session")
def neo4j_driver():
    """Live Neo4j driver — requires Docker to be running."""
    from neo4j import GraphDatabase
    driver = GraphDatabase.driver(
        settings.neo4j_uri,
        auth=settings.neo4j_auth,
    )
    driver.verify_connectivity()
    yield driver
    # Teardown: remove all test data to avoid cross-test contamination
    with driver.session() as session:
        session.run("MATCH (n) DETACH DELETE n")
    driver.close()


@pytest.fixture(scope="session")
def neo4j_session(neo4j_driver):
    """Single Neo4j session for the test session."""
    with neo4j_driver.session() as session:
        yield session


@pytest.fixture(scope="session")
def chroma_client():
    """Live ChromaDB HTTP client — requires Docker to be running."""
    from chromadb import HttpClient as ChromaHttpClient
    return ChromaHttpClient(
        host=settings.chroma_host,
        port=settings.chroma_port,
    )


@pytest.fixture(scope="session")
def redis_client():
    """Live Redis client — requires Docker to be running."""
    import redis as redis_lib
    return redis_lib.from_url(settings.redis_url, decode_responses=True)


# ── Sample UIR fixtures ───────────────────────────────────────────────────────

@pytest.fixture
def sample_parameter() -> Parameter:
    return Parameter(name="id", type_name="Long")


@pytest.fixture
def sample_logic_unit(sample_parameter) -> LogicUnit:
    return LogicUnit(
        geid="e7d2c8a1f3b50942",
        fqn="com.example.auth.UserService.getUser",
        kind="method",
        parameters=[sample_parameter],
        return_type="User",
        body_text="return userRepo.findById(id).orElseThrow();",
        docstring="Retrieves a user by their unique ID.",
        calls=["com.example.repo.UserRepository.findById"],
        file_path="src/main/java/com/example/auth/UserService.java",
        start_line=10,
        end_line=12,
    )


@pytest.fixture
def sample_component(sample_logic_unit) -> Component:
    return Component(
        geid="a1b2c3d4e5f60718",
        fqn="com.example.auth.UserService",
        kind="class",
        implements=["com.example.auth.IUserService"],
        extends=None,
        logic_units=[sample_logic_unit],
        file_path="src/main/java/com/example/auth/UserService.java",
        start_line=5,
        end_line=50,
    )


@pytest.fixture
def sample_module(sample_component) -> Module:
    return Module(
        geid="f6e5d4c3b2a10987",
        name="user-service",
        group_id="com.example",
        artifact_id="user-service",
        version="1.0.0",
        language="java",
        components=[sample_component],
        dependencies=[],
    )


@pytest.fixture
def sample_project(sample_module) -> Project:
    return Project(
        geid="0a1b2c3d4e5f6789",
        name="user-service",
        url="https://github.com/example/user-service",
        branch="main",
        modules=[sample_module],
    )


# ── Path fixtures ─────────────────────────────────────────────────────────────

@pytest.fixture
def fixtures_dir() -> Path:
    return Path(__file__).parent / "fixtures"


@pytest.fixture
def java_fixture_path(fixtures_dir) -> Path:
    return fixtures_dir / "UserService.java"


@pytest.fixture
def pom_fixture_path(fixtures_dir) -> Path:
    return fixtures_dir / "pom.xml"
