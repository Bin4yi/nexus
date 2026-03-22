"""
parsers/java_parser.py
Java AST parser using Tree-sitter — extracts all code entities into UIR objects.
Extended for enterprise-scale: extracts THROWS, INSTANTIATES, IMPLEMENTS (multi-interface),
INJECTS (field-level @Autowired/@Reference), @Override detection, nested classes,
and is_event_handler flag for WSO2 Observer pattern.
"""
from __future__ import annotations
from pathlib import Path
from typing import Optional
import re

import tree_sitter_java as tsjava
from tree_sitter import Language, Parser, Node

from parsers.uir import (
    Project, Module, Component, LogicUnit,
    Parameter, DependencyEdge, FieldDeclaration
)
from parsers.geid import generate_geid
from parsers.fqn_builder import build_component_fqn, build_logic_unit_fqn, build_call_fqn
from parsers.javadoc_parser import JavadocParser

# Build the language object once at module level
JAVA_LANGUAGE = Language(tsjava.language())
_parser = Parser(JAVA_LANGUAGE)
_javadoc_parser = JavadocParser()

# Injection annotations (Tier 2: INJECTS edge)
_INJECT_ANNOTATIONS = {"@Autowired", "@Inject", "@Resource", "@Reference", "@OSGiService"}

# WSO2 / Spring event handler base types
_EVENT_HANDLER_BASES = {
    "AbstractEventHandler", "EventHandler", "IdentityEventHandler",
    "AbstractIdentityHandler", "UserOperationEventListener",
    "AbstractUserOperationEventListener",
}

# HTTP client types for REMOTE_CALLS signal
_HTTP_CLIENT_TYPES = {
    "RestTemplate", "WebClient", "HttpClient", "FeignClient",
    "OkHttpClient", "CloseableHttpClient",
}


class JavaParser:
    """
    Parses a single Java source file into UIR objects using Tree-sitter.
    """

    def parse_file(self, file_path: Path, repo_name: str) -> list[Component]:
        """
        Parse a .java file and return all Components (classes/interfaces/enums).

        Args:
            file_path: Absolute path to the .java file
            repo_name: Short repository name for GEID generation

        Returns:
            List of Component UIR objects with nested LogicUnits
        """
        source = file_path.read_bytes()
        tree = _parser.parse(source)
        root = tree.root_node

        package_name = self._extract_package(root, source)
        imports = self._extract_imports(root, source)

        components = []
        for node in self._iter_type_declarations(root):
            component = self._parse_type_declaration(
                node, source, package_name, repo_name, str(file_path), imports
            )
            if component:
                components.append(component)
        return components

    # ── Package & imports ─────────────────────────────────────────────────────

    def _extract_package(self, root: Node, source: bytes) -> str:
        for child in root.children:
            if child.type == "package_declaration":
                for c in child.children:
                    if c.type in ("scoped_identifier", "identifier"):
                        return source[c.start_byte:c.end_byte].decode("utf-8")
        return ""

    def _extract_imports(self, root: Node, source: bytes) -> dict[str, str]:
        """Returns {simple_class_name: fully_qualified_name}"""
        imports: dict[str, str] = {}
        for child in root.children:
            if child.type == "import_declaration":
                for c in child.children:
                    if c.type in ("scoped_identifier", "identifier"):
                        fqn = source[c.start_byte:c.end_byte].decode("utf-8")
                        simple = fqn.rsplit(".", 1)[-1]
                        imports[simple] = fqn
        return imports

    # ── Type declarations ─────────────────────────────────────────────────────

    def _iter_type_declarations(self, root: Node):
        """Yield all top-level and nested class/interface/enum/annotation declarations."""
        for child in root.children:
            if child.type in (
                "class_declaration",
                "interface_declaration",
                "enum_declaration",
                "annotation_type_declaration",
            ):
                yield child

    def _iter_nested_type_declarations(self, body: Node):
        """Yield nested type declarations inside a class body."""
        for child in body.children:
            if child.type in (
                "class_declaration",
                "interface_declaration",
                "enum_declaration",
                "annotation_type_declaration",
            ):
                yield child

    def _parse_type_declaration(
        self,
        node: Node,
        source: bytes,
        package_name: str,
        repo_name: str,
        file_path: str,
        imports: dict[str, str],
    ) -> Optional[Component]:
        name = self._get_identifier(node, source)
        if not name:
            return None

        kind_map = {
            "class_declaration": "class",
            "interface_declaration": "interface",
            "enum_declaration": "enum",
            "annotation_type_declaration": "annotation",
        }
        kind = kind_map.get(node.type, "class")
        fqn = build_component_fqn(package_name, name)
        geid = generate_geid(repo_name, fqn)

        docstring = self._extract_preceding_javadoc(node, source)
        extends = self._extract_extends(node, source, imports)
        implements = self._extract_implements(node, source, imports)
        annotations = self._extract_annotations(node, source)

        # Extract component visibility and modifiers
        comp_visibility = "public"
        comp_is_abstract = False
        comp_is_final = False
        comp_modifiers = node.child_by_field_name("modifiers")
        if comp_modifiers:
            mod_text = source[comp_modifiers.start_byte:comp_modifiers.end_byte].decode("utf-8")
            comp_visibility = next((m for m in ["public", "protected", "private"] if m in mod_text), "package")
            comp_is_abstract = "abstract" in mod_text
            comp_is_final = "final" in mod_text

        # Determine if this is a WSO2/Spring event handler
        is_event_handler = self._is_event_handler(extends, implements)

        # Parse fields and methods inside the type body
        logic_units = []
        fields: list[FieldDeclaration] = []
        body = self._find_child_by_type(
            node, ("class_body", "interface_body", "enum_body", "annotation_type_body")
        )
        if body:
            for child in body.children:
                lu = self._parse_method_or_constructor(
                    child, source, package_name, name, repo_name, file_path, imports,
                    parent_extends=extends
                )
                if lu:
                    logic_units.append(lu)

            # Extract field declarations
            fields = self._extract_fields(body, source, imports)

        return Component(
            geid=geid,
            fqn=fqn,
            kind=kind,
            implements=implements,
            extends=extends,
            logic_units=logic_units,
            fields=fields,
            docstring=docstring,
            annotations=annotations,
            is_event_handler=is_event_handler,
            file_path=file_path,
            start_line=node.start_point[0] + 1,
            end_line=node.end_point[0] + 1,
            visibility=comp_visibility,
            is_abstract=comp_is_abstract,
            is_final=comp_is_final,
        )

    # ── Method / Constructor ──────────────────────────────────────────────────

    def _parse_method_or_constructor(
        self,
        node: Node,
        source: bytes,
        package_name: str,
        class_name: str,
        repo_name: str,
        file_path: str,
        imports: dict[str, str],
        parent_extends: Optional[str] = None,
    ) -> Optional[LogicUnit]:
        if node.type not in ("method_declaration", "constructor_declaration"):
            return None

        kind = "method" if node.type == "method_declaration" else "constructor"
        name = self._get_identifier(node, source)
        if not name:
            return None

        params = self._extract_parameters(node, source)
        param_types = [p.type_name for p in params]
        return_type = self._extract_return_type(node, source) if kind == "method" else None
        body_text = self._extract_body_text(node, source)
        calls = self._extract_calls(node, source, imports)
        annotations = self._extract_annotations(node, source)
        docstring = self._extract_preceding_javadoc(node, source)
        throws = self._extract_throws(node, source, imports)
        instantiates = self._extract_instantiations(node, source, imports)

        # Extract visibility and modifiers
        visibility = "package"
        is_static = False
        is_abstract = False
        is_final = False
        is_synchronized = False
        modifiers_node = node.child_by_field_name("modifiers")
        if modifiers_node:
            mod_text = source[modifiers_node.start_byte:modifiers_node.end_byte].decode("utf-8")
            visibility = next((m for m in ["public", "protected", "private"] if m in mod_text), "package")
            is_static = "static" in mod_text
            is_abstract = "abstract" in mod_text
            is_final = "final" in mod_text
            is_synchronized = "synchronized" in mod_text

        # Detect OSGi lifecycle role from annotations
        _OSGI_LIFECYCLE = {"Activate": "activate", "Deactivate": "deactivate", "Modified": "modified"}
        lifecycle_role: Optional[str] = None
        for ann in annotations:
            ann_name = ann.get("name", "")
            if ann_name in _OSGI_LIFECYCLE:
                lifecycle_role = _OSGI_LIFECYCLE[ann_name]
                break

        # Detect @Override → resolve overrides FQN from parent
        overrides: Optional[str] = None
        if any(a.get("name") == "Override" for a in annotations) and parent_extends:
            parent_simple = parent_extends.rsplit(".", 1)[-1]
            overrides = f"{parent_extends}.{name}" if "." in parent_extends else f"{parent_simple}.{name}"

        javadoc_data = _javadoc_parser.parse(docstring) if docstring else {}
        return_doc = javadoc_data.get("return")
        throws_doc = javadoc_data.get("throws", [])
        see_refs = javadoc_data.get("see", [])
        deprecated = "deprecated" in javadoc_data

        param_docs = javadoc_data.get("params", {})
        for p in params:
            if p.name in param_docs:
                p.doc = param_docs[p.name]

        fqn = build_logic_unit_fqn(package_name, class_name, name, param_types)
        geid = generate_geid(repo_name, fqn)

        return LogicUnit(
            geid=geid,
            fqn=fqn,
            kind=kind,
            parameters=params,
            return_type=return_type,
            body_text=body_text,
            docstring=javadoc_data.get("description", docstring),
            return_doc=return_doc,
            throws_doc=throws_doc,
            see_refs=see_refs,
            deprecated=deprecated,
            annotations=annotations,
            calls=calls,
            throws=throws,
            overrides=overrides,
            instantiates=instantiates,
            file_path=file_path,
            start_line=node.start_point[0] + 1,
            end_line=node.end_point[0] + 1,
            visibility=visibility,
            is_static=is_static,
            is_abstract=is_abstract,
            is_final=is_final,
            is_synchronized=is_synchronized,
            lifecycle_role=lifecycle_role,
        )

    # ── Field extraction ──────────────────────────────────────────────────────

    def _extract_fields(
        self, body: Node, source: bytes, imports: dict[str, str]
    ) -> list[FieldDeclaration]:
        """Extract all field declarations from a class body."""
        fields = []
        for child in body.children:
            if child.type == "field_declaration":
                annotations = self._extract_annotations(child, source)
                type_name = None
                for c in child.children:
                    if c.type in (
                        "type_identifier", "generic_type", "array_type",
                        "scoped_type_identifier", "integral_type",
                        "floating_point_type", "boolean_type",
                    ):
                        raw = source[c.start_byte:c.end_byte].decode("utf-8")
                        simple = raw.split("<")[0].strip()
                        base_fqn = imports.get(simple, simple)
                        # Preserve generic parameters: List<User> → java.util.List<User>
                        if "<" in raw:
                            type_name = base_fqn + raw[len(simple):]
                        else:
                            type_name = imports.get(simple, raw)
                        break

                # Collect variable names (may declare multiple: int a, b;)
                for c in child.children:
                    if c.type == "variable_declarator":
                        var_name = self._get_identifier(c, source)
                        if var_name and type_name:
                            is_injected = any(
                                "@" + (a.get("name", "") if isinstance(a, dict) else a.lstrip("@").split("(")[0])
                                in _INJECT_ANNOTATIONS
                                for a in annotations
                            )
                            norm_annotations = [
                                a["name"] if isinstance(a, dict) else str(a)
                                for a in annotations
                            ]
                            fields.append(FieldDeclaration(
                                name=var_name,
                                type_name=type_name,
                                annotations=norm_annotations,
                                is_injected=is_injected,
                            ))
        return fields

    # ── Throws extraction ─────────────────────────────────────────────────────

    def _extract_throws(
        self, node: Node, source: bytes, imports: dict[str, str]
    ) -> list[str]:
        """Extract exception types from throws clause."""
        throws = []
        for child in node.children:
            if child.type == "throws":
                for c in child.children:
                    if c.type in ("type_identifier", "scoped_type_identifier"):
                        name = source[c.start_byte:c.end_byte].decode("utf-8")
                        throws.append(imports.get(name, name))
        return throws

    # ── Instantiation extraction ──────────────────────────────────────────────

    def _extract_instantiations(
        self, node: Node, source: bytes, imports: dict[str, str]
    ) -> list[str]:
        """Extract class names from `new X(...)` expressions."""
        result: list[str] = []
        self._walk_instantiations(node, source, imports, result)
        return list(dict.fromkeys(result))

    def _walk_instantiations(
        self, node: Node, source: bytes, imports: dict[str, str], acc: list[str]
    ):
        if node.type == "object_creation_expression":
            for child in node.children:
                if child.type in ("type_identifier", "generic_type"):
                    raw = source[child.start_byte:child.end_byte].decode("utf-8")
                    simple = raw.split("<")[0].strip()
                    acc.append(imports.get(simple, simple))
                    break
        for child in node.children:
            self._walk_instantiations(child, source, imports, acc)

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _get_identifier(self, node: Node, source: bytes) -> Optional[str]:
        for child in node.children:
            if child.type == "identifier":
                return source[child.start_byte:child.end_byte].decode("utf-8")
        return None

    def _extract_parameters(self, node: Node, source: bytes) -> list[Parameter]:
        params = []
        fp = self._find_child_by_type(node, ("formal_parameters",))
        if not fp:
            return params
        for child in fp.children:
            if child.type in ("formal_parameter", "spread_parameter"):
                name = None
                type_name = None
                param_annotations: list[dict] = []
                for c in child.children:
                    if c.type == "identifier":
                        name = source[c.start_byte:c.end_byte].decode("utf-8")
                    elif c.type in (
                        "type_identifier", "integral_type", "floating_point_type",
                        "boolean_type", "void_type", "array_type", "generic_type",
                        "scoped_type_identifier",
                    ):
                        type_name = source[c.start_byte:c.end_byte].decode("utf-8")
                    elif c.type in ("marker_annotation", "annotation"):
                        ann = self._parse_annotation(c, source)
                        if ann:
                            param_annotations.append(ann)
                if name and type_name:
                    params.append(Parameter(name=name, type_name=type_name, annotations=param_annotations))
        return params

    def _extract_return_type(self, node: Node, source: bytes) -> Optional[str]:
        for child in node.children:
            if child.type in (
                "type_identifier", "integral_type", "floating_point_type",
                "boolean_type", "void_type", "array_type", "generic_type",
                "scoped_type_identifier",
            ):
                return source[child.start_byte:child.end_byte].decode("utf-8")
        return None

    def _extract_body_text(self, node: Node, source: bytes) -> str:
        body = self._find_child_by_type(node, ("block",))
        if body:
            return source[body.start_byte:body.end_byte].decode("utf-8", errors="replace")
        return ""

    def _extract_calls(
        self, node: Node, source: bytes, imports: dict[str, str]
    ) -> list[str]:
        calls = []
        self._walk_calls(node, source, imports, calls)
        return list(dict.fromkeys(calls))  # deduplicate preserving order

    def _walk_calls(
        self, node: Node, source: bytes, imports: dict[str, str], acc: list[str]
    ):
        if node.type == "method_invocation":
            method_name = None
            receiver = None
            for child in node.children:
                if child.type == "identifier":
                    if method_name is None:
                        method_name = source[child.start_byte:child.end_byte].decode()
                    else:
                        receiver = method_name
                        method_name = source[child.start_byte:child.end_byte].decode()
            if method_name:
                receiver_fqn = imports.get(receiver) if receiver else None
                fqn = build_call_fqn(receiver, method_name, receiver_fqn)
                acc.append(fqn)
        for child in node.children:
            self._walk_calls(child, source, imports, acc)

    def _parse_annotation(self, node: Node, source: bytes) -> dict:
        """Parse an annotation node into a structured dict."""
        name_node = node.child_by_field_name("name")
        if name_node:
            name = source[name_node.start_byte:name_node.end_byte].decode("utf-8")
        else:
            name = self._get_identifier(node, source)
        if not name:
            return {}

        result: dict = {"name": name}

        args_node = node.child_by_field_name("arguments")
        if args_node:
            raw_args = source[args_node.start_byte:args_node.end_byte].decode("utf-8", errors="replace").strip()
            if raw_args.startswith("("):
                raw_args = raw_args[1:]
            if raw_args.endswith(")"):
                raw_args = raw_args[:-1]
            raw_args = raw_args.strip()
            if raw_args:
                # Single string: @Value("${key}") or @Path("/api/v2")
                m = re.match(r'^["\']([^"\']+)["\']$', raw_args)
                if m:
                    result["value"] = m.group(1)
                else:
                    # Named key=value pairs: @Reference(cardinality = MANDATORY)
                    attrs: dict = {}
                    for pair_m in re.finditer(
                        r'(\w+)\s*=\s*(?:"([^"]*)"|\'([^\']*)\'|([A-Za-z0-9_.]+))',
                        raw_args,
                    ):
                        attr_name = pair_m.group(1)
                        attr_value = pair_m.group(2) or pair_m.group(3) or pair_m.group(4) or ""
                        attrs[attr_name] = attr_value
                    if attrs:
                        result["attrs"] = attrs  # don't overwrite result["name"] (annotation class)
                    elif raw_args:
                        result["value"] = raw_args[:200]
        return result

    def _extract_annotations(self, node: Node, source: bytes) -> list[dict]:
        annotations = []
        for child in node.children:
            if child.type in ("marker_annotation", "annotation"):
                ann = self._parse_annotation(child, source)
                if ann:
                    annotations.append(ann)
            elif child.type == "modifiers":
                # Annotations on class/method declarations are wrapped in a modifiers node
                for mod_child in child.children:
                    if mod_child.type in ("marker_annotation", "annotation"):
                        ann = self._parse_annotation(mod_child, source)
                        if ann:
                            annotations.append(ann)
        return annotations

    def _extract_preceding_javadoc(self, node: Node, source: bytes) -> str:
        """Find /** ... */ block comment immediately preceding this node."""
        prev = node.prev_sibling
        while prev and prev.type in ("modifiers",):
            prev = prev.prev_sibling
        if prev and prev.type == "block_comment":
            text = source[prev.start_byte:prev.end_byte].decode("utf-8")
            if text.startswith("/**"):
                return text
        return ""

    def _extract_extends(
        self, node: Node, source: bytes, imports: dict[str, str]
    ) -> Optional[str]:
        for child in node.children:
            if child.type == "superclass":
                for c in child.children:
                    if c.type in ("type_identifier", "generic_type"):
                        name = source[c.start_byte:c.end_byte].decode("utf-8")
                        simple = name.split("<")[0].strip()
                        return imports.get(simple, name)
        return None

    def _extract_implements(
        self, node: Node, source: bytes, imports: dict[str, str]
    ) -> list[str]:
        """
        Extract all implemented interfaces. Handles both:
         - super_interfaces → type_list → type_identifier (standard)
         - super_interfaces → interface_type_list → type_identifier (tree-sitter variant)
        """
        result = []
        for child in node.children:
            if child.type in ("super_interfaces", "extends_interfaces"):
                # Walk all children looking for type_identifier / generic_type
                self._collect_type_names(child, source, imports, result)
        return result

    def _collect_type_names(
        self, node: Node, source: bytes, imports: dict[str, str], acc: list[str]
    ):
        """Recursively collect type names from a node tree."""
        if node.type in ("type_identifier", "generic_type"):
            raw = source[node.start_byte:node.end_byte].decode("utf-8")
            simple = raw.split("<")[0].strip()
            acc.append(imports.get(simple, raw))
            return  # don't recurse into generics
        for child in node.children:
            self._collect_type_names(child, source, imports, acc)

    def _is_event_handler(
        self, extends: Optional[str], implements: list[str]
    ) -> bool:
        """Check if a class is a WSO2/Spring event handler."""
        if extends:
            simple = extends.rsplit(".", 1)[-1]
            if simple in _EVENT_HANDLER_BASES:
                return True
        for iface in implements:
            simple = iface.rsplit(".", 1)[-1]
            if simple in _EVENT_HANDLER_BASES:
                return True
        return False

    def _find_child_by_type(self, node: Node, types: tuple) -> Optional[Node]:
        for child in node.children:
            if child.type in types:
                return child
        return None
