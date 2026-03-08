"""
parsers/fqn_builder.py
Builds Fully Qualified Names from Tree-sitter AST context.

Computes the canonical ``package.ClassName.methodName(ParamType)`` string
that serves as the primary lookup key across Neo4j and ChromaDB.
Used by ``JavaParser`` during Stage 2 (Extract).
"""
from __future__ import annotations
from typing import Optional


def build_component_fqn(package_name: str, class_name: str) -> str:
    """
    Build a class/interface/enum FQN.

    Args:
        package_name: e.g. "com.example.auth"
        class_name:   e.g. "UserService"

    Returns:
        "com.example.auth.UserService"
    """
    if package_name:
        return f"{package_name}.{class_name}"
    return class_name


def build_logic_unit_fqn(
    package_name: str,
    class_name: str,
    method_name: str,
    param_types: Optional[list[str]] = None,
) -> str:
    """
    Build a method/constructor FQN.

    Overloaded methods are disambiguated by parameter types.
    If no param_types provided, method name alone is used (simpler, less precise).

    Args:
        package_name: e.g. "com.example.auth"
        class_name:   e.g. "UserService"
        method_name:  e.g. "getUser"
        param_types:  e.g. ["Long"] — used for overload disambiguation

    Returns:
        "com.example.auth.UserService.getUser" (no params)
        "com.example.auth.UserService.getUser(Long)" (with params)
    """
    base = build_component_fqn(package_name, class_name)
    if param_types:
        params_str = ",".join(param_types)
        return f"{base}.{method_name}({params_str})"
    return f"{base}.{method_name}"


def build_call_fqn(
    receiver_type: Optional[str],
    method_name: str,
    package_hint: Optional[str] = None,
) -> str:
    """
    Build a best-effort FQN for a method invocation call target.
    Used to populate LogicUnit.calls[] — may not always be fully qualified
    if the receiver type cannot be resolved from the AST alone.

    Args:
        receiver_type: The inferred type of the receiver object, e.g. "UserRepository"
        method_name:   The called method name, e.g. "findById"
        package_hint:  Import package hint if available

    Returns:
        Best-effort FQN e.g. "UserRepository.findById"
    """
    if receiver_type:
        if package_hint:
            return f"{package_hint}.{receiver_type}.{method_name}"
        return f"{receiver_type}.{method_name}"
    return method_name
