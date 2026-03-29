"""
parsers/uir.py
Universal Intermediate Representation — Pydantic models for all parsed
Java entities.

Hierarchy: ``Project → Module → Component → LogicUnit``.
Used as the canonical transport format between the parser, linker, loader,
and chunker stages.  Every UIR object carries a GEID for graph ↔ vector
bridging.
"""
from __future__ import annotations
from datetime import datetime
from typing import Optional
from pydantic import BaseModel, Field


class Parameter(BaseModel):
    """A single method/constructor parameter."""
    name: str
    type_name: str
    doc: Optional[str] = None           # from @param Javadoc tag
    annotations: list[dict] = Field(default_factory=list)   # e.g. [{"name": "QueryParam", "value": "client_id"}]


class FieldDeclaration(BaseModel):
    """A field declared in a class/interface body."""
    name: str
    type_name: str                      # simple or qualified type
    annotations: list[str] = Field(default_factory=list)
    is_injected: bool = False           # @Autowired / @Inject / @Resource / @Reference


class LogicUnit(BaseModel):
    """
    A single method or constructor. The atomic unit of code semantics.
    Maps to a Neo4j :LogicUnit node and two ChromaDB vectors (logic + intent).
    """
    geid: str                           # sha256(repo::fqn)[:16]
    fqn: str                            # com.example.auth.UserService.getUser
    kind: str                           # "method" | "constructor"
    parameters: list[Parameter] = Field(default_factory=list)
    return_type: Optional[str] = None
    body_text: str = ""                 # raw source body
    docstring: str = ""                 # Javadoc description block
    return_doc: Optional[str] = None    # @return tag
    throws_doc: list[str] = Field(default_factory=list)     # @throws tags
    see_refs: list[str] = Field(default_factory=list)       # @see references
    deprecated: bool = False            # @deprecated present
    annotations: list[dict] = Field(default_factory=list)   # e.g. [{"name": "Override"}]
    calls: list[str] = Field(default_factory=list)          # FQNs of called methods
    property_reads: list[str] = Field(default_factory=list)  # keys read via getProperty(key)
    property_writes: list[str] = Field(default_factory=list) # keys written via addProperty(key,v)
    throws: list[str] = Field(default_factory=list)         # exception types in throws clause
    overrides: Optional[str] = None                         # parent method FQN if @Override
    instantiates: list[str] = Field(default_factory=list)   # class names from `new X()`
    file_path: str = ""
    start_line: int = 0
    end_line: int = 0
    # v2: visibility and modifiers (security analysis)
    visibility: str = "package"         # "public" | "protected" | "private" | "package"
    is_static: bool = False
    is_abstract: bool = False
    is_final: bool = False
    is_synchronized: bool = False
    lifecycle_role: Optional[str] = None  # "activate" | "deactivate" | "modified" (OSGi)


class Component(BaseModel):
    """
    A Java class, interface, enum, or annotation type.
    Maps to a Neo4j :Component node.
    """
    geid: str
    fqn: str                            # com.example.auth.UserService
    kind: str                           # "class" | "interface" | "enum" | "annotation"
    implements: list[str] = Field(default_factory=list)    # FQNs of implemented interfaces
    extends: Optional[str] = None      # FQN of parent class
    logic_units: list[LogicUnit] = Field(default_factory=list)
    fields: list[FieldDeclaration] = Field(default_factory=list)   # declared fields
    docstring: str = ""
    annotations: list[dict] = Field(default_factory=list)   # e.g. [{"name": "Component"}]
    is_event_handler: bool = False      # extends AbstractEventHandler / implements EventHandler
    file_path: str = ""
    start_line: int = 0
    end_line: int = 0
    # v2: visibility and modifiers
    visibility: str = "public"
    is_abstract: bool = False
    is_final: bool = False


class DependencyEdge(BaseModel):
    """A resolved Maven dependency edge between two modules."""
    target_group_id: str
    target_artifact_id: str
    target_version: Optional[str] = None
    scope: str = "compile"             # compile | test | provided | runtime


class Module(BaseModel):
    """
    A Maven artifact (one pom.xml).
    Maps to a Neo4j :Module node.
    """
    geid: str
    name: str                          # artifact display name
    group_id: str
    artifact_id: str
    version: str = "UNKNOWN"
    language: str = "java"
    components: list[Component] = Field(default_factory=list)
    dependencies: list[DependencyEdge] = Field(default_factory=list)


class Project(BaseModel):
    """
    A single Git repository.
    Maps to a Neo4j :Project node.
    """
    geid: str
    name: str                          # short repo name (used in GEID generation)
    url: str
    branch: str = "main"
    modules: list[Module] = Field(default_factory=list)
    last_indexed: Optional[datetime] = None
