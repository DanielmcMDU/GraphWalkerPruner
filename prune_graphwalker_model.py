#!/usr/bin/env python3
"""Derive a product-specific GraphWalker model from a 150% JSON model.

* unlabelled vertices and edges are retained;
* ``FEATURE = A, B`` is treated as an OR-list and retained when A or B is selected;
* variables owned only by deselected features are identified;
* obsolete initialisation/reset/action statements are removed;
* references to removed variables are removed from Boolean guards; and
* editor metadata is synchronized with the executable graph.

"""

from __future__ import annotations

import copy
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, Sequence


FEATURE_PATTERN = re.compile(
    r"FEATURE\s*=\s*"
    r"([A-Z0-9_]+(?:\s*,\s*[A-Z0-9_]+)*)",
    re.IGNORECASE,
)
IDENTIFIER_PATTERN = re.compile(r"(?<![\w.])(?:global\.)?[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*")
ASSIGNMENT_PATTERN = re.compile(
    r"^\s*((?:global\.)?[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*)\s*"
    r"(?<![=!<>])=(?!=)"
)

KEYWORDS = {
    "true",
    "false",
    "null",
    "undefined",
    "global",
    "return",
    "if",
    "else",
    "new",
}

class PruningError(ValueError):
    """Raised when strict pruning cannot produce a trustworthy model."""


def canonical_variable(name: str) -> str:
    """Normalize ``global.x`` and ``x`` to the same variable key."""

    name = name.strip()
    return name[7:] if name.startswith("global.") else name


def extract_features_from_requirements(requirements: Any) -> set[str]:
    """Extract feature names from ``FEATURE = A, B`` annotations.

    A comma-separated annotation is an OR-list: an element belongs to every
    listed feature and is retained when any one of them is selected. Repeated
    FEATURE annotations are accepted as the same OR-list for compatibility.
    """

    features: set[str] = set()
    if not requirements:
        return features

    for requirement in requirements:
        if not isinstance(requirement, str):
            continue
        for feature_list in FEATURE_PATTERN.findall(requirement):
            features.update(
                feature.strip().upper()
                for feature in feature_list.split(",")
                if feature.strip()
            )
    return features


def should_keep_element(element: dict[str, Any], selected_features: set[str]) -> bool:
    """Keep unlabelled elements or elements owned by any selected feature."""

    features = extract_features_from_requirements(element.get("requirements", []))
    return not features or bool(features & selected_features)


def extract_variables(text: str | None) -> set[str]:
    """Return normalized variable identifiers referenced by a guard/action."""

    if not text:
        return set()

    variables: set[str] = set()
    for match in IDENTIFIER_PATTERN.finditer(text):
        token = match.group(0)
        normalized = canonical_variable(token)
        if normalized.lower() in KEYWORDS:
            continue
        # Exclude function names so helper calls are not classified as variables.
        suffix = text[match.end() :].lstrip()
        if suffix.startswith("("):
            continue
        variables.add(normalized)
    return variables


def assigned_variable(statement: str) -> str | None:
    """Return the normalized left-hand variable of a simple assignment."""

    match = ASSIGNMENT_PATTERN.match(statement)
    return canonical_variable(match.group(1)) if match else None


def split_action_statements(action_text: str) -> list[str]:
    """Split JavaScript-like actions at semicolons outside strings/brackets.
    """

    statements: list[str] = []
    current: list[str] = []
    quote: str | None = None
    escaped = False
    depth = 0

    for character in action_text:
        current.append(character)
        if escaped:
            escaped = False
            continue
        if character == "\\" and quote:
            escaped = True
            continue
        if quote:
            if character == quote:
                quote = None
            continue
        if character in {"'", '"'}:
            quote = character
        elif character in "([{":
            depth += 1
        elif character in ")]}":
            depth = max(0, depth - 1)
        elif character == ";" and depth == 0:
            statement = "".join(current).strip()
            if statement:
                statements.append(statement)
            current = []

    trailing = "".join(current).strip()
    if trailing:
        statements.append(trailing)
    return statements


def join_action_statements(statements: Sequence[str]) -> str:
    """Reassemble statements as one semicolon-delimited action string."""

    normalized: list[str] = []
    for statement in statements:
        value = statement.strip()
        if not value:
            continue
        normalized.append(value if value.endswith(";") else f"{value};")
    return " ".join(normalized)


# ---------------------------------------------------------------------------
# Guard parser
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Token:
    kind: str
    value: str


TOKEN_PATTERN = re.compile(
    r"\s*(?:"
    r"(?P<AND>&&)|(?P<OR>\|\|)|(?P<EQ>==)|(?P<NE>!=)|"
    r"(?P<LE><=)|(?P<GE>>=)|(?P<LT><)|(?P<GT>>)|(?P<NOT>!)|"
    r"(?P<LPAREN>\()|(?P<RPAREN>\))|"
    r"(?P<BOOL>\b(?:true|false)\b)|"
    r"(?P<NUMBER>-?(?:\d+(?:\.\d*)?|\.\d+))|"
    r"(?P<STRING>'(?:\\.|[^'\\])*'|\"(?:\\.|[^\"\\])*\")|"
    r"(?P<IDENT>(?:global\.)?[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*)"
    r")",
    re.IGNORECASE,
)


class GuardNode:
    pass


@dataclass(frozen=True)
class Atom(GuardNode):
    text: str
    variables: frozenset[str]


@dataclass(frozen=True)
class Boolean(GuardNode):
    value: bool


@dataclass(frozen=True)
class Not(GuardNode):
    child: GuardNode


@dataclass(frozen=True)
class And(GuardNode):
    children: tuple[GuardNode, ...]


@dataclass(frozen=True)
class Or(GuardNode):
    children: tuple[GuardNode, ...]


@dataclass(frozen=True)
class Removed(GuardNode):
    """Internal marker for a guard fragment belonging to a removed feature."""


REMOVED = Removed()


def tokenize_guard(text: str) -> list[Token]:
    """Tokenize the Boolean subset used by GraphWalker guards."""

    tokens: list[Token] = []
    position = 0
    while position < len(text):
        match = TOKEN_PATTERN.match(text, position)
        if not match:
            if text[position:].strip() == "":
                break
            raise PruningError(
                f"Unsupported guard syntax near: {text[position:position + 40]!r}"
            )
        kind = match.lastgroup
        if kind is None:
            raise PruningError("Could not tokenize guard expression")
        tokens.append(Token(kind, match.group(kind)))
        position = match.end()
    tokens.append(Token("EOF", ""))
    return tokens


class GuardParser:
    """Small recursive-descent parser for &&, ||, ! and comparisons."""

    def __init__(self, text: str):
        self.tokens = tokenize_guard(text)
        self.position = 0

    @property
    def current(self) -> Token:
        return self.tokens[self.position]

    def consume(self, kind: str | None = None) -> Token:
        token = self.current
        if kind is not None and token.kind != kind:
            raise PruningError(f"Expected {kind}, found {token.kind} ({token.value!r})")
        self.position += 1
        return token

    def parse(self) -> GuardNode:
        node = self.parse_or()
        if self.current.kind != "EOF":
            raise PruningError(f"Unexpected guard token: {self.current.value!r}")
        return node

    def parse_or(self) -> GuardNode:
        children = [self.parse_and()]
        while self.current.kind == "OR":
            self.consume()
            children.append(self.parse_and())
        return children[0] if len(children) == 1 else Or(tuple(children))

    def parse_and(self) -> GuardNode:
        children = [self.parse_not()]
        while self.current.kind == "AND":
            self.consume()
            children.append(self.parse_not())
        return children[0] if len(children) == 1 else And(tuple(children))

    def parse_not(self) -> GuardNode:
        if self.current.kind == "NOT":
            self.consume()
            return Not(self.parse_not())
        return self.parse_primary()

    def parse_primary(self) -> GuardNode:
        if self.current.kind == "LPAREN":
            self.consume()
            node = self.parse_or()
            self.consume("RPAREN")
            return node
        return self.parse_atom()

    def parse_atom(self) -> GuardNode:
        allowed = {"IDENT", "BOOL", "NUMBER", "STRING", "EQ", "NE", "LE", "GE", "LT", "GT"}
        parts: list[str] = []
        while self.current.kind in allowed:
            parts.append(self.consume().value)
        if not parts:
            raise PruningError(f"Expected guard predicate, found {self.current.value!r}")
        text = " ".join(parts)
        if len(parts) == 1 and parts[0].lower() in {"true", "false"}:
            return Boolean(parts[0].lower() == "true")
        return Atom(text=text, variables=frozenset(extract_variables(text)))


def simplify_guard(node: GuardNode, removed_variables: set[str]) -> GuardNode:
    """Remove feature-owned predicates and apply Boolean identities.

    A removed operand is dropped from its surrounding conjunction/disjunction.
    This is equivalent to the neutral value of that operator (true for AND,
    false for OR). A fully removed nested group propagates upward as REMOVED.
    """

    if isinstance(node, Atom):
        return REMOVED if node.variables & removed_variables else node
    if isinstance(node, (Boolean, Removed)):
        return node
    if isinstance(node, Not):
        child = simplify_guard(node.child, removed_variables)
        if isinstance(child, Removed):
            return REMOVED
        if isinstance(child, Boolean):
            return Boolean(not child.value)
        if isinstance(child, Not):
            return child.child
        return Not(child)

    children = [simplify_guard(child, removed_variables) for child in node.children]
    children = [child for child in children if not isinstance(child, Removed)]
    if not children:
        return REMOVED

    if isinstance(node, And):
        if any(isinstance(child, Boolean) and not child.value for child in children):
            return Boolean(False)
        children = [child for child in children if not (isinstance(child, Boolean) and child.value)]
        if not children:
            return Boolean(True)
        flattened: list[GuardNode] = []
        for child in children:
            flattened.extend(child.children if isinstance(child, And) else [child])
        return flattened[0] if len(flattened) == 1 else And(tuple(flattened))

    if any(isinstance(child, Boolean) and child.value for child in children):
        return Boolean(True)
    children = [child for child in children if not (isinstance(child, Boolean) and not child.value)]
    if not children:
        return Boolean(False)
    flattened = []
    for child in children:
        flattened.extend(child.children if isinstance(child, Or) else [child])
    return flattened[0] if len(flattened) == 1 else Or(tuple(flattened))


def guard_node_variables(node: GuardNode) -> set[str]:
    """Collect variables from an already parsed guard subtree."""

    if isinstance(node, Atom):
        return set(node.variables)
    if isinstance(node, Not):
        return guard_node_variables(node.child)
    if isinstance(node, (And, Or)):
        return set().union(*(guard_node_variables(child) for child in node.children))
    return set()


def guard_precedence(node: GuardNode) -> int:
    if isinstance(node, Or):
        return 1
    if isinstance(node, And):
        return 2
    if isinstance(node, Not):
        return 3
    return 4


def render_guard(node: GuardNode, parent_precedence: int = 0) -> str:
    """Render the simplified AST using GraphWalker-compatible operators."""

    if isinstance(node, Boolean):
        return "true" if node.value else "false"
    if isinstance(node, Atom):
        return node.text
    if isinstance(node, Removed):
        raise PruningError("Cannot render a completely removed guard")
    if isinstance(node, Not):
        child = render_guard(node.child, guard_precedence(node))
        if guard_precedence(node.child) < guard_precedence(node):
            child = f"({child})"
        return f"!{child}"

    operator = " && " if isinstance(node, And) else " || "
    precedence = guard_precedence(node)
    parts: list[str] = []
    for child in node.children:
        rendered = render_guard(child, precedence)
        if guard_precedence(child) < precedence:
            rendered = f"({rendered})"
        parts.append(rendered)
    result = operator.join(parts)
    return f"({result})" if precedence < parent_precedence else result


def rewrite_guard(guard: str, removed_variables: set[str]) -> tuple[str, set[str]]:
    """Rewrite one guard and return its text and variables that were removed."""

    original_variables = extract_variables(guard)
    affected = original_variables & removed_variables
    if not affected:
        return guard.strip(), set()

    simplified = simplify_guard(GuardParser(guard).parse(), removed_variables)
    # A retained edge whose entire guard belonged to removed variables has no
    # remaining product-specific constraint and therefore becomes unguarded.
    rewritten = "true" if isinstance(simplified, Removed) else render_guard(simplified)
    return rewritten, affected


# ---------------------------------------------------------------------------
# Analysis and transformation
# ---------------------------------------------------------------------------


@dataclass
class VariableInfo:
    feature_owners: set[str] = field(default_factory=set)
    common_owner: bool = False
    assigned_locations: list[str] = field(default_factory=list)
    referenced_locations: list[str] = field(default_factory=list)


@dataclass
class PruningPlan:
    selected_features: set[str]
    all_features: set[str]
    removed_features: set[str]
    variable_index: dict[str, VariableInfo]
    removable_variables: set[str]


def iter_models_and_elements(model_data: dict[str, Any]) -> Iterator[tuple[dict[str, Any], str, dict[str, Any]]]:
    """Yield models and executable vertices/edges with stable descriptions."""

    for model in model_data.get("models", []):
        model_name = model.get("name", "<unnamed model>")
        yield model, f"model:{model_name}", model
        for collection in ("vertices", "edges"):
            for element in model.get(collection, []):
                name = element.get("name", element.get("id", "<unnamed>"))
                yield model, f"{model_name}/{collection[:-1]}:{name}", element


def action_entries(element: dict[str, Any]) -> list[str]:
    actions = element.get("actions", [])
    return [action for action in actions if isinstance(action, str) and action.strip()]


def is_reset_element(element: dict[str, Any]) -> bool:
    return "reset" in str(element.get("name", "")).lower()


def build_pruning_plan(
    model_data: dict[str, Any],
    selected_features: set[str],
    common_variables: set[str],
) -> PruningPlan:
    """Analyze the untouched 150% model before structural information is lost."""

    all_features: set[str] = set()
    variable_index: dict[str, VariableInfo] = {}

    def info(variable: str) -> VariableInfo:
        return variable_index.setdefault(variable, VariableInfo())

    for _model, location, element in iter_models_and_elements(model_data):
        features = extract_features_from_requirements(element.get("requirements", []))
        all_features.update(features)
        retained = not features or bool(features & selected_features)

        for action in action_entries(element):
            for statement in split_action_statements(action):
                assigned = assigned_variable(statement)
                variables = extract_variables(statement)
                if assigned:
                    item = info(assigned)
                    item.assigned_locations.append(location)
                    if features and not is_reset_element(element):
                        item.feature_owners.update(features)
                    # Model-level actions and reset actions contain bulk copies
                    # of every feature variable. They are consumers of the
                    # ownership decision, not evidence that all data is common.
                    elif retained and element.get("sourceVertexId") and not is_reset_element(element):
                        item.common_owner = True
                for variable in variables:
                    info(variable).referenced_locations.append(location)

        guard = element.get("guard")
        if isinstance(guard, str):
            for variable in extract_variables(guard):
                info(variable).referenced_locations.append(f"{location}/guard")

    for variable in common_variables:
        info(variable).common_owner = True

    removed_features = all_features - selected_features
    removable_variables: set[str] = set()
    for variable, item in variable_index.items():
        if not item.feature_owners:
            continue
        selected_owner = bool(item.feature_owners & selected_features)
        removed_owner = bool(item.feature_owners & removed_features)
        if removed_owner and not selected_owner and not item.common_owner:
            removable_variables.add(variable)

    return PruningPlan(
        selected_features=selected_features,
        all_features=all_features,
        removed_features=removed_features,
        variable_index=variable_index,
        removable_variables=removable_variables,
    )


def prune_action_list(
    actions: Any,
    removed_variables: set[str],
    location: str,
    strict: bool,
) -> list[str]:
    """Remove obsolete statements while preserving action-list organization."""

    if not isinstance(actions, list):
        return []

    result: list[str] = []
    for action in actions:
        if not isinstance(action, str) or not action.strip():
            continue

        kept_statements: list[str] = []
        for statement in split_action_statements(action):
            target = assigned_variable(statement)
            variables = extract_variables(statement)
            affected = variables & removed_variables

            if target and target in removed_variables:
                continue

            # A statement that writes retained data from removed data cannot be
            # safely simplified automatically. Failing in strict mode prevents
            # the pruner from silently changing the system semantics.
            if affected:
                message = (
                    f"{location}: retained action references removed variable(s) "
                    f"{sorted(affected)}: {statement!r}"
                )
                if strict:
                    raise PruningError(message)
                continue
            kept_statements.append(statement)

        rebuilt = join_action_statements(kept_statements)
        if rebuilt:
            result.append(rebuilt)
    return result


def synchronize_editor(model: dict[str, Any]) -> None:
    """Mirror executable node/edge content into GraphWalker editor metadata."""

    vertices = {vertex["id"]: vertex for vertex in model.get("vertices", [])}
    edges = {edge["id"]: edge for edge in model.get("edges", [])}
    elements = model.get("editor", {}).get("elements", {})

    nodes = elements.get("nodes")
    if isinstance(nodes, list):
        synchronized_nodes = []
        for node in nodes:
            identifier = node.get("data", {}).get("id")
            if identifier not in vertices:
                continue
            data = node.setdefault("data", {})
            executable = vertices[identifier]
            for key in ("name", "sharedState", "requirements", "actions"):
                if key in executable:
                    data[key] = copy.deepcopy(executable[key])
                else:
                    data.pop(key, None)
            synchronized_nodes.append(node)
        elements["nodes"] = synchronized_nodes

    editor_edges = elements.get("edges")
    if isinstance(editor_edges, list):
        synchronized_edges = []
        for editor_edge in editor_edges:
            identifier = editor_edge.get("data", {}).get("id")
            if identifier not in edges:
                continue
            data = editor_edge.setdefault("data", {})
            executable = edges[identifier]
            for key in ("name", "guard", "requirements", "actions"):
                if key in executable:
                    data[key] = copy.deepcopy(executable[key])
                else:
                    data.pop(key, None)
            synchronized_edges.append(editor_edge)
        elements["edges"] = synchronized_edges


def prune_model(
    model_data: dict[str, Any],
    selected_features: set[str],
    config_data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return the validated product-specific model."""

    if not isinstance(model_data, dict):
        raise PruningError("The input JSON must contain an object at its root")
    if not isinstance(model_data.get("models"), list) or not model_data["models"]:
        raise PruningError("The input JSON must contain a non-empty 'models' list")

    selected_features = {
        str(feature).strip().upper()
        for feature in selected_features
        if str(feature).strip()
    }
    config_data = config_data or {}
    strict = bool(config_data.get("strict", True))
    common_variables = {
        canonical_variable(str(variable))
        for variable in config_data.get("common_variables", [])
    }

    result = copy.deepcopy(model_data)
    plan = build_pruning_plan(result, selected_features, common_variables)

    unknown_features = selected_features - plan.all_features
    if unknown_features:
        message = f"Selected features are not present in the model: {sorted(unknown_features)}"
        if strict:
            raise PruningError(message)

    pruned_models: list[dict[str, Any]] = []
    for model in result.get("models", []):
        if not isinstance(model, dict):
            raise PruningError("Every item in 'models' must be an object")
        model_name = model.get("name", "<unnamed model>")
        original_vertices = model.get("vertices", [])
        original_edges = model.get("edges", [])
        if not isinstance(original_vertices, list) or not isinstance(original_edges, list):
            raise PruningError(
                f"Model {model_name!r}: 'vertices' and 'edges' must be lists"
            )
        if any(not isinstance(item, dict) for item in original_vertices + original_edges):
            raise PruningError(
                f"Model {model_name!r}: every vertex and edge must be an object"
            )
        for collection_name, collection in (
            ("vertex", original_vertices),
            ("edge", original_edges),
        ):
            identifiers = [item.get("id") for item in collection]
            if any(not identifier for identifier in identifiers):
                raise PruningError(
                    f"Model {model_name!r}: every {collection_name} must have a non-empty id"
                )
            duplicates = sorted({identifier for identifier in identifiers if identifiers.count(identifier) > 1})
            if duplicates:
                raise PruningError(
                    f"Model {model_name!r}: duplicate {collection_name} id(s): {duplicates}"
                )

        kept_vertices = [
            vertex for vertex in original_vertices
            if should_keep_element(vertex, selected_features)
        ]
        kept_vertex_ids = {vertex["id"] for vertex in kept_vertices}

        kept_edges: list[dict[str, Any]] = []
        for edge in original_edges:
            keep = should_keep_element(edge, selected_features)
            keep = keep and edge.get("sourceVertexId") in kept_vertex_ids
            keep = keep and edge.get("targetVertexId") in kept_vertex_ids
            if keep:
                kept_edges.append(edge)

        connected_ids = {
            endpoint
            for edge in kept_edges
            for endpoint in (edge.get("sourceVertexId"), edge.get("targetVertexId"))
        }
        start_element_id = model.get("startElementId")
        final_vertices = [
            vertex for vertex in kept_vertices
            if vertex.get("id") in connected_ids
            or (start_element_id and vertex.get("id") == start_element_id)
        ]
        final_vertex_ids = {vertex["id"] for vertex in final_vertices}

        final_edges = [
            edge for edge in kept_edges
            if edge.get("sourceVertexId") in final_vertex_ids
            and edge.get("targetVertexId") in final_vertex_ids
        ]

        if start_element_id and start_element_id not in final_vertex_ids:
            raise PruningError(
                f"Model {model_name!r}: startElementId {start_element_id!r} was removed"
            )

        model["vertices"] = final_vertices
        model["edges"] = final_edges

        # Action and guard pruning is applied only after structural selection, so
        # feature-specific edges that are already gone do not need rewriting.
        model["actions"] = prune_action_list(
            model.get("actions", []),
            plan.removable_variables,
            f"model:{model_name}",
            strict,
        )
        for collection in ("vertices", "edges"):
            for element in model.get(collection, []):
                element_name = element.get("name", element.get("id", "<unnamed>"))
                location = f"{model_name}/{collection[:-1]}:{element_name}"
                if "actions" in element:
                    element["actions"] = prune_action_list(
                        element.get("actions", []),
                        plan.removable_variables,
                        location,
                        strict,
                    )
                guard = element.get("guard")
                if isinstance(guard, str) and guard.strip():
                    rewritten, affected = rewrite_guard(guard, plan.removable_variables)
                    if affected:
                        element["guard"] = rewritten

        synchronize_editor(model)
        if final_vertices or final_edges:
            pruned_models.append(model)

    if not pruned_models:
        raise PruningError(f"All models were removed for features {sorted(selected_features)}")

    result["models"] = pruned_models
    if result.get("selectedModelIndex", 0) >= len(pruned_models):
        result["selectedModelIndex"] = 0

    validate_model(result, plan.removable_variables, strict)
    return result


def validate_model(
    model_data: dict[str, Any],
    removed_variables: set[str],
    strict: bool,
) -> None:
    """Validate references and graph integrity after transformation."""

    errors: list[str] = []
    warnings: list[str] = []
    nonempty_start_models = 0

    for model in model_data.get("models", []):
        model_name = model.get("name", "<unnamed model>")
        vertex_ids = {vertex.get("id") for vertex in model.get("vertices", [])}
        edge_ids = {edge.get("id") for edge in model.get("edges", [])}
        start = model.get("startElementId")
        if start:
            nonempty_start_models += 1
            if start not in vertex_ids:
                errors.append(f"{model_name}: invalid startElementId {start!r}")

        for edge in model.get("edges", []):
            if edge.get("sourceVertexId") not in vertex_ids:
                errors.append(f"{model_name}/{edge.get('name')}: missing source vertex")
            if edge.get("targetVertexId") not in vertex_ids:
                errors.append(f"{model_name}/{edge.get('name')}: missing target vertex")

        elements = model.get("editor", {}).get("elements", {})
        editor_node_ids = {
            item.get("data", {}).get("id") for item in elements.get("nodes", [])
        }
        editor_edge_ids = {
            item.get("data", {}).get("id") for item in elements.get("edges", [])
        }
        if editor_node_ids and editor_node_ids != vertex_ids:
            errors.append(f"{model_name}: editor nodes do not match executable vertices")
        if editor_edge_ids and editor_edge_ids != edge_ids:
            errors.append(f"{model_name}: editor edges do not match executable edges")

        for _owner, location, element in iter_models_and_elements({"models": [model]}):
            guard = element.get("guard")
            if isinstance(guard, str) and guard.strip():
                remaining = extract_variables(guard) & removed_variables
                if remaining:
                    errors.append(f"{location}: guard retains removed variables {sorted(remaining)}")
                try:
                    GuardParser(guard).parse()
                except PruningError as error:
                    errors.append(f"{location}: invalid rewritten guard: {error}")
            for action in action_entries(element):
                remaining = extract_variables(action) & removed_variables
                if remaining:
                    errors.append(f"{location}: action retains removed variables {sorted(remaining)}")

    if nonempty_start_models == 0:
        errors.append("No retained model has a non-empty startElementId")
    elif nonempty_start_models > 1:
        warnings.append(
            f"{nonempty_start_models} models have non-empty startElementId values; expected one main model"
        )

    if errors and strict:
        raise PruningError("Validation failed:\n- " + "\n- ".join(errors))


def main() -> None:
    if len(sys.argv) != 4:
        print(
            "Usage: python prune_graphwalker_model.py "
            "<input_model.json> <config.json> <output_model.json>"
        )
        sys.exit(1)

    input_model_path = Path(sys.argv[1])
    config_path = Path(sys.argv[2])
    output_model_path = Path(sys.argv[3])

    try:
        with input_model_path.open("r", encoding="utf-8") as file:
            model_data = json.load(file)
        with config_path.open("r", encoding="utf-8") as file:
            config_data = json.load(file)

        selected_features = {
            str(feature).strip().upper()
            for feature in config_data.get("selected_features", [])
            if str(feature).strip()
        }
        pruned_model = prune_model(model_data, selected_features, config_data)

        output_model_path.parent.mkdir(parents=True, exist_ok=True)

        with output_model_path.open("w", encoding="utf-8", newline="\n") as file:
            json.dump(pruned_model, file, indent=2, ensure_ascii=False)
            file.write("\n")
        print(f"Pruned model written to: {output_model_path}")
    except (OSError, json.JSONDecodeError, PruningError) as error:
        print(f"Pruning failed: {error}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
