"""
parsers/osgi_parser.py
OSGi @Component / @Reference resolver — Sprint 2.

Scans Java source files for OSGi Declarative Services annotations:
  - @Component  → marks a concrete service implementation
  - @Reference  → marks a field injection of an OSGi service interface

Builds ``[:RESOLVES_TO]`` edges from injected Interface Components to
their concrete Implementation Components (the @Component that provides it).

Design note:
    OSGi resolution is a runtime concept — the container wires interfaces to
    implementations based on the @Component service declarations.  We approximate
    this at parse time by matching the declared ``service`` attribute of @Component
    to the type declared on @Reference fields.

    Fallback: if no ``service`` attribute is present we use IMPLEMENTS edges
    already loaded in Neo4j to resolve the mapping at load time.
"""
from __future__ import annotations
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# ── Regex patterns ────────────────────────────────────────────────────────────

# @Component with optional service = {SomeInterface.class, ...} attribute
_COMPONENT_RE = re.compile(
    r"@Component\s*(?:\([^)]*\))?",
    re.DOTALL,
)

# Extract service attribute value(s): service = {Foo.class, Bar.class}
_SERVICE_ATTR_RE = re.compile(
    r"service\s*=\s*\{?([^})\n]+)\}?",
    re.IGNORECASE,
)

# Extract .class references from the service attribute
_CLASS_REF_RE = re.compile(r"([\w.]+)\.class")

# @Reference field annotations
_REFERENCE_RE = re.compile(
    r"@Reference(?:\([^)]*\))?\s+(?:(?:private|protected|public|volatile|transient)\s+)*"
    r"([\w.<>]+)\s+(\w+)\s*;",
    re.DOTALL,
)

# Detect the class FQN declared in a Java file (package + class name)
_PACKAGE_RE = re.compile(r"^\s*package\s+([\w.]+)\s*;", re.MULTILINE)
_CLASS_NAME_RE = re.compile(
    r"(?:public\s+)?(?:abstract\s+)?(?:final\s+)?(?:class|interface|enum)\s+(\w+)",
)
_IMPLEMENTS_RE = re.compile(
    r"(?:class\s+\w+\s+(?:extends\s+[\w.]+\s+)?)?implements\s+([^{]+)",
)


@dataclass
class OSGiComponentInfo:
    """A class annotated with @Component."""
    fqn: str                         # fully-qualified class name
    geid: str                        # Neo4j GEID
    service_interfaces: list[str]    # declared service interfaces (simple names)


@dataclass
class OSGiResolutionEdge:
    """A [:RESOLVES_TO] edge: injected interface → concrete implementation."""
    interface_fqn: str               # FQN of the injected interface type
    implementation_fqn: str          # FQN of the @Component implementation
    reference_field: str             # field name that carries the @Reference


class OSGiParser:
    """
    Scans all Java files in a repository for OSGi service wiring.

    Returns a list of ``OSGiResolutionEdge`` objects that the graph loader
    uses to create ``[:RESOLVES_TO]`` edges.
    """

    def parse_components(
        self,
        java_files: list[Path],
        components: list,  # list[Component] from UIR
    ) -> list[OSGiComponentInfo]:
        """
        Identify all classes annotated with @Component and extract
        their declared service interfaces.
        """
        comp_info: list[OSGiComponentInfo] = []
        fqn_to_comp = {c.fqn: c for c in components if c.fqn}

        for jf in java_files:
            try:
                source = jf.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue

            if "@Component" not in source:
                continue

            pkg = _PACKAGE_RE.search(source)
            pkg_name = pkg.group(1) if pkg else ""

            cls_m = _CLASS_NAME_RE.search(source)
            if not cls_m:
                continue
            cls_name = cls_m.group(1)
            fqn = f"{pkg_name}.{cls_name}" if pkg_name else cls_name

            # Extract service interfaces declared in @Component(service=...)
            services: list[str] = []
            for comp_m in _COMPONENT_RE.finditer(source):
                ann_text = comp_m.group(0)
                svc_m = _SERVICE_ATTR_RE.search(ann_text)
                if svc_m:
                    for cls_ref in _CLASS_REF_RE.finditer(svc_m.group(1)):
                        services.append(cls_ref.group(1).strip())

            # Fallback: use implements clause
            if not services:
                impl_m = _IMPLEMENTS_RE.search(source)
                if impl_m:
                    raw = impl_m.group(1)
                    for iface in re.split(r"\s*,\s*", raw.strip()):
                        simple = iface.split("<")[0].strip()
                        if simple:
                            services.append(simple)

            comp = fqn_to_comp.get(fqn)
            geid = comp.geid if comp else fqn

            if services:
                comp_info.append(OSGiComponentInfo(
                    fqn=fqn,
                    geid=geid,
                    service_interfaces=services,
                ))
                logger.debug(
                    "OSGi @Component: %s → services=%s", fqn, services,
                )

        logger.info(
            "Found %d @Component service declarations", len(comp_info),
        )
        return comp_info

    def build_resolution_edges(
        self,
        java_files: list[Path],
        components: list,
        osgi_components: list[OSGiComponentInfo],
    ) -> list[OSGiResolutionEdge]:
        """
        Scan @Reference field injections and match them to @Component implementations.

        Resolution strategy:
        1. Match the field's declared type (simple name) against the ``service_interfaces``
           of known @Component declarations.
        2. Return a ``OSGiResolutionEdge`` for each resolved pairing.
        """
        edges: list[OSGiResolutionEdge] = []

        # Build lookup: simple interface name → list of implementing FQNs
        iface_to_impls: dict[str, list[str]] = {}
        for osgi_comp in osgi_components:
            for svc in osgi_comp.service_interfaces:
                simple = svc.rsplit(".", 1)[-1]
                iface_to_impls.setdefault(simple, []).append(osgi_comp.fqn)

        # Build FQN lookup for components
        fqn_to_comp = {c.fqn: c for c in components if c.fqn}

        for jf in java_files:
            try:
                source = jf.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue

            if "@Reference" not in source:
                continue

            pkg = _PACKAGE_RE.search(source)
            pkg_name = pkg.group(1) if pkg else ""

            for ref_m in _REFERENCE_RE.finditer(source):
                field_type = ref_m.group(1).split("<")[0].strip()
                field_name = ref_m.group(2)

                # Simple type name → look up implementations
                simple_type = field_type.rsplit(".", 1)[-1]
                impl_fqns = iface_to_impls.get(simple_type, [])

                # Best-effort FQN for the interface
                iface_fqn = field_type if "." in field_type else f"{pkg_name}.{field_type}" if pkg_name else field_type

                for impl_fqn in impl_fqns:
                    edges.append(OSGiResolutionEdge(
                        interface_fqn=iface_fqn,
                        implementation_fqn=impl_fqn,
                        reference_field=field_name,
                    ))
                    logger.debug(
                        "OSGi RESOLVES_TO: %s → %s (via @Reference %s)",
                        iface_fqn, impl_fqn, field_name,
                    )

        logger.info("Built %d OSGi [:RESOLVES_TO] edges", len(edges))
        return edges
