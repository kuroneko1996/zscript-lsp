from __future__ import annotations

from lsprotocol import types
from tree_sitter import Node

from index_manager import IndexManager
from scope import Scope, SymbolEntry
from symbol_info import SymbolInfo
from utils import node_text, node_to_range


class SemanticValidator:
    """
    Выполняет семантическую проверку файла ZScript:
    - отслеживает лексические области видимости (Scope);
    - выводит типы переменных (включая let, new("..."), вызовы методов и цепочки точек);
    - проверяет существование идентификаторов и членов классов/структур.
    """

    def __init__(
        self,
        index_manager: IndexManager,
        builtin_identifiers: list[str] | None = None,
    ) -> None:
        self.index_manager = index_manager
        self.builtin_identifiers = builtin_identifiers or []
        self.diagnostics: list[types.Diagnostic] = []
        self.scope_stack: list[Scope] = []
        self.current_class: str | None = None

    def validate(self, source: str) -> list[types.Diagnostic]:
        """Запускает проверку AST и возвращает список диагностик."""
        tree = self.index_manager.parser.parse(bytes(source, "utf-8"))
        self.diagnostics = []
        self.scope_stack = [self._build_global_scope()]
        self.current_class = None

        self._walk_top_level(tree.root_node, source)
        return self.diagnostics

    # --- Работа с областями видимости и наследованием ---

    def _build_global_scope(self) -> Scope:
        global_scope = Scope()
        for name in self.builtin_identifiers:
            global_scope.define_symbol(name, None)

        for sym_key, entries in self.index_manager.symbol_index.items():
            first = entries[0]
            if first.kind in (
                types.CompletionItemKind.Class,
                types.CompletionItemKind.Struct,
            ):
                global_scope.define_symbol(sym_key, sym_key)
            else:
                global_scope.define_symbol(sym_key, first.type_name)
        return global_scope

    def _get_members_with_inheritance(self, class_key: str) -> list[SymbolInfo]:
        result = []
        seen_members: set[str] = set()
        visited_classes: set[str] = set()

        curr = class_key.lower()
        while curr and curr not in visited_classes:
            visited_classes.add(curr)
            for member in self.index_manager.class_members.get(curr, []):
                m_key = member.orig_name.lower()
                if m_key not in seen_members:
                    seen_members.add(m_key)
                    result.append(member)
            next_parent = self.index_manager.class_parents.get(curr, "")
            if not next_parent and curr != "object" and "object" not in visited_classes:
                next_parent = "object"
            curr = next_parent
        return result

    def _push_scope(self) -> Scope:
        new_scope = Scope(parent=self.scope_stack[-1])
        self.scope_stack.append(new_scope)
        return new_scope

    def _pop_scope(self) -> None:
        if len(self.scope_stack) > 1:
            self.scope_stack.pop()

    def _define(self, name: str, type_str: str | None) -> None:
        self.scope_stack[-1].define_symbol(name, type_str)

    def _lookup(self, name: str) -> SymbolEntry | None:
        return self.scope_stack[-1].lookup_symbol(name)

    # --- Вывод типов выражений ---

    def _resolve_node_type(self, expr: Node | None, source: str) -> str | None:
        if not expr:
            return None

        if expr.type == "identifier":
            entry = self._lookup(node_text(expr, source))
            return entry.type if entry else None

        if expr.type in ("function_expression", "typecast_expression"):
            fn_node = expr.child_by_field_name("function") or expr.child_by_field_name(
                "type"
            )
            if not fn_node:
                return None

            if fn_node.type == "identifier":
                fn_name = node_text(fn_node, source).lower()
                if fn_name == "new":
                    args = expr.children_by_field_name(
                        "argument"
                    ) or expr.children_by_field_name("target")
                    for arg in args:
                        raw = node_text(arg, source).strip("'").strip('"').lower()
                        if self.index_manager.is_known_class_or_struct(raw):
                            return raw
                    return None

                if self.index_manager.is_known_class_or_struct(fn_name):
                    return fn_name

                entry = self._lookup(fn_name)
                return entry.type if entry else None

            if fn_node.type == "member_access_expression":
                return self._resolve_node_type(fn_node, source)

        if expr.type == "member_access_expression" and expr.children:
            obj_node = expr.children[0]
            mem_node = expr.child_by_field_name("member")
            owner_type = self._resolve_node_type(obj_node, source)
            if owner_type and mem_node:
                mem_name = node_text(mem_node, source).lower()
                if mem_name == "default":
                    return owner_type
                for m in self._get_members_with_inheritance(owner_type):
                    if m.orig_name.lower() == mem_name:
                        return m.type_name

        return None

    # --- Обход верхнего уровня (классы, структуры, методы) ---

    def _walk_top_level(self, node: Node, source: str) -> None:
        if node.type in ("class_definition", "struct_definition"):
            name_node = node.child_by_field_name("name")
            prev_class = self.current_class
            if name_node:
                self.current_class = node_text(name_node, source).lower()
            for ch in node.children:
                self._walk_top_level(ch, source)
            self.current_class = prev_class
            return

        if node.type == "method_declaration":
            self._validate_method(node, source)
            return

        for ch in node.children:
            self._walk_top_level(ch, source)

    def _validate_method(self, method_node: Node, source: str) -> None:
        body = next((c for c in method_node.children if c.type == "block"), None)
        if not body:
            return

        self._push_scope()

        # 1. Добавляем self, super и члены текущего класса + родителей
        if self.current_class:
            self._define("self", self.current_class)
            parent_cls = self.index_manager.class_parents.get(self.current_class)
            self._define("super", parent_cls)
            for m in self._get_members_with_inheritance(self.current_class):
                self._define(m.orig_name, m.type_name)

        # 2. Добавляем параметры метода
        param_list = next(
            (c for c in method_node.children if c.type == "parameter_list"), None
        )
        if param_list:
            p_types = param_list.children_by_field_name("type")
            p_names = param_list.children_by_field_name("name")
            for t_node, n_node in zip(p_types, p_names):
                p_name = node_text(n_node, source)
                p_type = node_text(t_node, source).strip("@").lower()
                self._define(p_name, p_type)

        # 3. Проверяем тело метода
        for ch in body.children:
            self._check_node(ch, source)

        self._pop_scope()

    # --- Проверка инструкций и выражений внутри метода ---

    def _check_node(self, node: Node, source: str) -> None:
        if node.type == "ERROR" or node.is_missing:
            return

        handler = getattr(self, f"_visit_{node.type}", self._visit_generic)
        handler(node, source)

    def _visit_generic(self, node: Node, source: str) -> None:
        for ch in node.children:
            self._check_node(ch, source)

    def _visit_block(self, node: Node, source: str) -> None:
        self._push_scope()
        for ch in node.children:
            self._check_node(ch, source)
        self._pop_scope()

    def _visit_for_statement(self, node: Node, source: str) -> None:
        self._push_scope()
        for ch in node.children:
            self._check_node(ch, source)
        self._pop_scope()

    def _visit_foreach_statement(self, node: Node, source: str) -> None:
        right_node = node.child_by_field_name("right")
        if right_node:
            self._check_node(right_node, source)

        self._push_scope()
        for field_name in ("left", "value"):
            f_node = node.child_by_field_name(field_name)
            if f_node and f_node.type == "identifier":
                self._define(node_text(f_node, source), None)

        body_node = node.child_by_field_name("body")
        if body_node:
            self._check_node(body_node, source)
        self._pop_scope()

    def _visit_declaration_statement(self, node: Node, source: str) -> None:
        t_node = node.child_by_field_name("type")
        explicit_type = node_text(t_node, source).strip("@").lower() if t_node else None

        for child in node.children:
            if child.type == "identifier":
                self._define(node_text(child, source), explicit_type)
            elif child.type == "subscript_expression":
                arr_name = child.children[0] if child.children else None
                if arr_name and arr_name.type == "identifier":
                    self._define(node_text(arr_name, source), explicit_type)
            elif child.type == "assignment_expression":
                right = child.child_by_field_name("right")
                if right:
                    self._check_node(right, source)

                left = child.child_by_field_name("left")
                if not left:
                    continue

                var_type = explicit_type or self._resolve_node_type(right, source)
                if left.type == "identifier":
                    self._define(node_text(left, source), var_type)
                elif left.type == "array_expression":
                    elems = [el for el in left.children if el.type == "identifier"]
                    type_parts = (
                        [p.strip() for p in var_type.split(",")]
                        if var_type and "," in var_type
                        else []
                    )
                    for idx, el in enumerate(elems):
                        el_type = type_parts[idx] if idx < len(type_parts) else var_type
                        self._define(node_text(el, source), el_type)

    def _visit_const_definition(self, node: Node, source: str) -> None:
        val_node = node.child_by_field_name("value") or node.child_by_field_name(
            "contents"
        )
        if val_node:
            self._check_node(val_node, source)

        name_node = node.child_by_field_name("name")
        t_node = node.child_by_field_name("type")
        c_type = node_text(t_node, source).strip("@").lower() if t_node else None

        if name_node:
            self._define(node_text(name_node, source), c_type)

    def _visit_const_array_definition(self, node: Node, source: str) -> None:
        self._visit_const_definition(node, source)

    def _visit_member_access_expression(self, node: Node, source: str) -> None:
        if not node.children:
            return

        obj_node = node.children[0]
        self._check_node(obj_node, source)
        mem_node = node.child_by_field_name("member")
        if not mem_node:
            return

        target_mem = (
            mem_node.children[0]
            if mem_node.type == "member_access_expression" and mem_node.children
            else mem_node
        )
        owner_type = self._resolve_node_type(obj_node, source)
        if (
            owner_type
            and self.index_manager.is_known_class_or_struct(owner_type)
            and target_mem.type == "identifier"
        ):
            orig_mem = node_text(target_mem, source)
            mem_lower = orig_mem.lower()
            members = self._get_members_with_inheritance(owner_type)
            if not any(m.orig_name.lower() == mem_lower for m in members):
                self.diagnostics.append(
                    types.Diagnostic(
                        range=node_to_range(target_mem),
                        message=f"Unknown member '{orig_mem}' on type '{owner_type}'",
                        severity=types.DiagnosticSeverity.Error,
                    )
                )
                return

        # Проверка вложенной цепочки точек (например: owner.Default.Height)
        if mem_node.type == "member_access_expression":
            next_obj_type = (
                owner_type
                if target_mem.type == "identifier"
                and node_text(target_mem, source).lower() == "default"
                else next(
                    (
                        m.type_name
                        for m in self._get_members_with_inheritance(owner_type or "")
                        if m.orig_name.lower() == node_text(target_mem, source).lower()
                    ),
                    None,
                )
            )
            tail_mem = mem_node.child_by_field_name("member")
            if (
                next_obj_type
                and self.index_manager.is_known_class_or_struct(next_obj_type)
                and tail_mem
                and tail_mem.type == "identifier"
            ):
                tail_orig = node_text(tail_mem, source)
                tail_lower = tail_orig.lower()
                tail_members = self._get_members_with_inheritance(next_obj_type)
                if not any(m.orig_name.lower() == tail_lower for m in tail_members):
                    self.diagnostics.append(
                        types.Diagnostic(
                            range=node_to_range(tail_mem),
                            message=f"Unknown member '{tail_orig}' on type '{next_obj_type}'",
                            severity=types.DiagnosticSeverity.Error,
                        )
                    )

    def _visit_identifier(self, node: Node, source: str) -> None:
        if self._is_ignored_identifier(node):
            return

        orig_text = node_text(node, source)
        if not self._lookup(orig_text):
            self.diagnostics.append(
                types.Diagnostic(
                    range=node_to_range(node),
                    message=f"Unknown identifier '{orig_text}'",
                    severity=types.DiagnosticSeverity.Error,
                )
            )

    @staticmethod
    def _is_ignored_identifier(node: Node) -> bool:
        parent = node.parent
        if not parent:
            return True

        if parent.type == "member_access_expression":
            member_node = parent.child_by_field_name("member")
            if member_node and member_node.id == node.id:
                return True

        if parent.type in (
            "declaration_statement",
            "parameter_list",
            "class_type",
            "const_definition",
            "const_array_definition",
            "enum_declaration",
            "enum_declaration_value",
            "state_label",
            "frame_sprite",
            "control_statement",
        ):
            return True

        if (
            parent.type == "assignment_expression"
            and parent.parent
            and parent.parent.type == "declaration_statement"
        ):
            left = parent.child_by_field_name("left")
            if left and left.id == node.id:
                return True

        if (
            parent.type == "array_expression"
            and parent.parent
            and parent.parent.type == "assignment_expression"
            and parent.parent.parent
            and parent.parent.parent.type == "declaration_statement"
        ):
            left = parent.parent.child_by_field_name("left")
            if left and left.id == parent.id:
                return True

        if parent.type == "foreach_statement":
            left = parent.child_by_field_name("left")
            val = parent.child_by_field_name("value")
            if (left and left.id == node.id) or (val and val.id == node.id):
                return True

        # ruff: disable[SIM103]
        # Именованный аргумент функции: Unmorph(self, force: ...)
        if (
            node.next_sibling
            and node.next_sibling.type == ":"
            and parent.type != "ternary_expression"
        ):
            return True

        return False
