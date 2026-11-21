from pathlib import Path
from typing import TYPE_CHECKING

from lsprotocol import types
from tree_sitter import Node

from symbol_info import SymbolInfo
from utils import (
    extract_signature,
    find_owner_node,
    node_text,
    node_to_range,
    resolve_include_path,
)

if TYPE_CHECKING:
    from index_manager import IndexManager


class ZScriptVisitor:
    """
    Dispatches traversal logic for ZScript AST nodes to the IndexManager.
    This implements the Visitor pattern to adhere to the Single Responsibility Principle.
    """

    def __init__(self, manager: "IndexManager") -> None:
        self.manager = manager

    def visit_root_node(
        self,
        node: Node,
        source: str,
        uri: str,
        workspace_root: Path | None,
        diagnostics: list[types.Diagnostic],
    ) -> None:
        for child in node.children:
            self.generic_visit(child, source, uri, workspace_root, diagnostics)

    def generic_visit(
        self,
        node: Node,
        source: str,
        uri: str,
        workspace_root: Path | None,
        diagnostics: list[types.Diagnostic],
    ) -> None:
        if node.type == "ERROR" or node.is_missing:
            diagnostics.append(
                types.Diagnostic(
                    range=node_to_range(node),
                    message="ZScript syntax error",
                    severity=types.DiagnosticSeverity.Error,
                )
            )
            return
        # Dispatch to the specific handler for the node type
        handler = getattr(self, f"visit_{node.type}", self.visit_generic)
        handler(node, source, uri, workspace_root, diagnostics)

    def visit_class_definition(
        self,
        node: Node,
        source: str,
        uri: str,
        workspace_root: Path | None,
        diagnostics: list[types.Diagnostic],
    ) -> None:
        name_node = node.child_by_field_name("name")
        if not name_node:
            return
        # Handle inheritance
        inherit_node = node.child_by_field_name("inherit")
        if inherit_node and inherit_node.next_named_sibling:
            cls_key = node_text(name_node, source).lower()
            parent_node = inherit_node.next_named_sibling
            parent_key = node_text(parent_node, source).lower()
            self.manager.class_parents[cls_key] = parent_key

        self.manager.add_symbol(
            source=source,
            uri=uri,
            name_node=name_node,
            decl_node=node,
            kind=types.CompletionItemKind.Class,
        )
        for child in node.children:
            self.generic_visit(child, source, uri, workspace_root, diagnostics)

    def visit_struct_definition(
        self,
        node: Node,
        source: str,
        uri: str,
        workspace_root: Path | None,
        diagnostics: list[types.Diagnostic],
    ) -> None:
        name_node = node.child_by_field_name("name")
        if name_node:
            self.manager.add_symbol(
                source=source,
                uri=uri,
                name_node=name_node,
                decl_node=node,
                kind=types.CompletionItemKind.Struct,
            )
        for child in node.children:
            self.generic_visit(child, source, uri, workspace_root, diagnostics)

    def visit_enum_declaration(
        self,
        node: Node,
        source: str,
        uri: str,
        workspace_root: Path | None,
        diagnostics: list[types.Diagnostic],
    ) -> None:
        name_node = node.child_by_field_name("name")
        if name_node:
            self.manager.add_symbol(
                source=source,
                uri=uri,
                name_node=name_node,
                decl_node=node,
                kind=types.CompletionItemKind.Enum,
            )
        for child in node.children:
            self.generic_visit(child, source, uri, workspace_root, diagnostics)

    def visit_method_declaration(
        self,
        node: Node,
        source: str,
        uri: str,
        workspace_root: Path | None,
        diagnostics: list[types.Diagnostic],
    ) -> None:
        name_node = node.child_by_field_name("name")
        if name_node:
            self.manager.add_symbol(
                source=source,
                uri=uri,
                name_node=name_node,
                decl_node=node,
                kind=types.CompletionItemKind.Method,
            )
        for child in node.children:
            self.generic_visit(child, source, uri, workspace_root, diagnostics)

    def visit_variable_declaration(
        self,
        node: Node,
        source: str,
        uri: str,
        workspace_root: Path | None,
        diagnostics: list[types.Diagnostic],
    ) -> None:
        # index only class/struct fields, not local variables
        if find_owner_node(node):
            for child in node.children_by_field_name("name"):
                self.manager.add_symbol(
                    source=source,
                    uri=uri,
                    name_node=child,
                    decl_node=node,
                    kind=types.CompletionItemKind.Variable,
                )

    def visit_enum_declaration_value(
        self,
        node: Node,
        source: str,
        uri: str,
        workspace_root: Path | None,
        diagnostics: list[types.Diagnostic],
    ) -> None:
        name_node = node.child_by_field_name("name")
        if name_node:
            self.manager.add_symbol(
                source=source,
                uri=uri,
                name_node=name_node,
                decl_node=node,
                kind=types.CompletionItemKind.EnumMember,
            )
        for child in node.children:
            self.generic_visit(child, source, uri, workspace_root, diagnostics)

    def visit_state_label(
        self,
        node: Node,
        source: str,
        uri: str,
        workspace_root: Path | None,
        diagnostics: list[types.Diagnostic],
    ) -> None:
        name_node = node.child_by_field_name("name")
        if name_node:
            self.manager.add_symbol(
                source=source,
                uri=uri,
                name_node=name_node,
                decl_node=node,
                kind=types.CompletionItemKind.Event,
            )
        for child in node.children:
            self.generic_visit(child, source, uri, workspace_root, diagnostics)

    def visit_const_array_definition(
        self,
        node: Node,
        source: str,
        uri: str,
        workspace_root: Path | None,
        diagnostics: list[types.Diagnostic],
    ) -> None:
        self.visit_const_definition(node, source, uri, workspace_root, diagnostics)

    def visit_const_definition(
        self,
        node: Node,
        source: str,
        uri: str,
        workspace_root: Path | None,
        diagnostics: list[types.Diagnostic],
    ) -> None:
        const_name = node.child_by_field_name("name")
        if not const_name:
            for ch in node.children:
                if ch.type == "identifier":
                    const_name = ch
                    break
                if ch.type == "assignment_expression":
                    left = ch.child_by_field_name("left")
                    if left and left.type == "identifier":
                        const_name = left
                        break
        if const_name:
            self.manager.add_symbol(
                source=source,
                uri=uri,
                name_node=const_name,
                decl_node=node,
                kind=types.CompletionItemKind.Constant,
            )
        for child in node.children:
            self.generic_visit(child, source, uri, workspace_root, diagnostics)

    def visit_flagdef_declaration(
        self,
        node: Node,
        source: str,
        uri: str,
        workspace_root: Path | None,
        diagnostics: list[types.Diagnostic],
    ) -> None:
        flag_node = node.child_by_field_name("name") or node.child_by_field_name("flag")
        if not flag_node:
            flag_node = next(
                (ch for ch in node.children if ch.type == "identifier"), None
            )

        if flag_node:
            raw_flag = node_text(flag_node, source)
            full_flag = f"b{raw_flag}"
            key = full_flag.lower()
            owner_info = find_owner_node(node)
            owner_name = owner_info[1] if owner_info else None
            sig = extract_signature(node, source)

            info = SymbolInfo(
                orig_name=full_flag,
                kind=types.CompletionItemKind.Property,
                uri=uri,
                location=types.Location(uri=uri, range=node_to_range(flag_node)),
                signature=sig,
                owner=owner_name,
                type_name="bool",
            )
            self.manager.add_symbol(
                source,
                uri,
                flag_node,
                node,
                types.CompletionItemKind.Property,
            )
            self.manager.file_symbols[uri].add(key)

            if owner_name:
                owner_key = owner_name.lower()
                self.manager.class_members.setdefault(owner_key, []).append(info)
                self.manager.file_classes[uri].add(owner_key)

        for child in node.children:
            self.generic_visit(child, source, uri, workspace_root, diagnostics)

    def visit_include_definition(
        self,
        node: Node,
        source: str,
        uri: str,
        workspace_root: Path | None,
        diagnostics: list[types.Diagnostic],
    ) -> None:
        path_node = node.child_by_field_name("path")
        if path_node:
            inc_text = node_text(path_node, source)
            target_file = resolve_include_path(workspace_root, uri, inc_text)
            if target_file:
                self.manager.handle_include(
                    target_file.as_uri(), target_file, workspace_root
                )

    def visit_generic(
        self,
        node: Node,
        source: str,
        uri: str,
        workspace_root: Path | None,
        diagnostics: list[types.Diagnostic],
    ) -> None:
        # Fallback for unhandled nodes - ensures traversal continues
        for child in node.children:
            self.generic_visit(child, source, uri, workspace_root, diagnostics)
