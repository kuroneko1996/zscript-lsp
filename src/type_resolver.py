import re

from lsprotocol import types
from tree_sitter import Node, Parser

from index_manager import IndexManager
from symbol_info import SymbolInfo
from utils import node_text


class TypeResolver:
    def __init__(self, parser: Parser, index_manager: IndexManager) -> None:
        self.parser = parser
        self.index_manager = index_manager

    def find_enclosing_by_line(
        self, root: Node, line: int
    ) -> tuple[Node | None, Node | None]:
        found_method: Node | None = None
        found_owner: Node | None = None

        def walk(n: Node) -> None:
            nonlocal found_method, found_owner
            if n.start_point.row <= line <= n.end_point.row:
                if n.type in ("class_definition", "struct_definition"):
                    found_owner = n
                elif n.type == "method_declaration":
                    found_method = n
                for ch in n.children:
                    walk(ch)

        walk(root)
        return found_method, found_owner

    def resolve_symbol_at_point(
        self, source: str, point: tuple[int, int], node: Node
    ) -> list[SymbolInfo] | None:
        target_key = node_text(node, source).strip('"').strip("'").lower()
        if not target_key:
            return None

        parent = node.parent
        # 1. Вызов/доступ через точку: obj.Member или ClassName.Member
        if (
            parent
            and parent.type == "member_access_expression"
            and (member_node := parent.child_by_field_name("member"))
            and member_node.id == node.id
            and parent.children
        ):
            obj_ident = node_text(parent.children[0], source).lower()
            resolved_owner = self.resolve_identifier_type(obj_ident, source, point)
            if resolved_owner:
                members = self.index_manager.get_members_with_inheritance(
                    resolved_owner
                )
                if matched := [m for m in members if m.orig_name.lower() == target_key]:
                    return matched

        # 2. Вызов без точки внутри класса: DrawBarKeys()
        tree = self.parser.parse(bytes(source, "utf-8"))
        _, owner_node = self.find_enclosing_by_line(tree.root_node, point[0])
        if owner_node and (name_node := owner_node.child_by_field_name("name")):
            current_class = node_text(name_node, source).lower()
            members = self.index_manager.get_members_with_inheritance(current_class)
            if matched := [m for m in members if m.orig_name.lower() == target_key]:
                return [matched[0]]

        # 3. Глобальный индекс
        return self.index_manager.get_symbol(target_key)

    def resolve_identifier_type(
        self, ident_lower: str, source: str, point: tuple[int, int]
    ) -> str | None:
        if self.index_manager.is_known_class_or_struct(ident_lower):
            return ident_lower

        tree = self.parser.parse(bytes(source, "utf-8"))
        current_method, owner_node = self.find_enclosing_by_line(
            tree.root_node, point[0]
        )

        current_class: str | None = None
        if owner_node and (name_node := owner_node.child_by_field_name("name")):
            current_class = node_text(name_node, source).lower()

        if ident_lower == "self":
            return current_class
        if ident_lower == "super" and current_class:
            return self.index_manager.class_parents.get(current_class)

        if current_method:
            if p_type := self._resolve_from_params(current_method, ident_lower, source):
                return p_type
            if l_type := self._resolve_from_locals(
                current_method, ident_lower, source, current_class
            ):
                return l_type

        if current_class:
            for member in self.index_manager.get_members_with_inheritance(
                current_class
            ):
                if member.orig_name.lower() == ident_lower and member.type_name:
                    return member.type_name

        return None

    def _resolve_from_params(
        self, method_node: Node, ident_lower: str, source: str
    ) -> str | None:
        param_list = next(
            (c for c in method_node.children if c.type == "parameter_list"), None
        )
        if not param_list:
            return None
        for t_node, n_node in zip(
            param_list.children_by_field_name("type"),
            param_list.children_by_field_name("name"),
        ):
            if node_text(n_node, source).lower() == ident_lower:
                return node_text(t_node, source).strip("@").lower()
        return None

    def _resolve_from_locals(
        self,
        method_node: Node,
        ident_lower: str,
        source: str,
        current_class: str | None,
    ) -> str | None:
        def scan(n: Node) -> str | None:
            if n.type == "declaration_statement":
                t_node = n.child_by_field_name("type")
                t_str = node_text(t_node, source).strip("@").lower() if t_node else None
                for child in n.children:
                    if (
                        t_str
                        and child.type == "identifier"
                        and node_text(child, source).lower() == ident_lower
                    ):
                        return t_str
                    if child.type == "assignment_expression":
                        left = child.child_by_field_name("left")
                        right = child.child_by_field_name("right")
                        if left and node_text(left, source).lower() == ident_lower:
                            return t_str or self.infer_rhs_type(
                                right, source, current_class
                            )
            for ch in n.children:
                if res := scan(ch):
                    return res
            return None

        if ast_type := scan(method_node):
            return ast_type

        # Regex-откат для неполного AST при вводе текста
        method_text = node_text(method_node, source)
        if m_new := re.search(
            rf"\blet\s+{re.escape(ident_lower)}\s*=\s*new\s*\(\s*['\"]([^'\"]+)['\"]\s*\)",
            method_text,
            re.IGNORECASE,
        ):
            return m_new.group(1).lower()

        if m_call := re.search(
            rf"\blet\s+{re.escape(ident_lower)}\s*=\s*([a-zA-Z_]\w*)\.([a-zA-Z_]\w*)\s*\(",
            method_text,
            re.IGNORECASE,
        ):
            cls_n, meth_n = m_call.group(1).lower(), m_call.group(2).lower()
            owner_t = current_class if cls_n == "self" else cls_n
            for m in self.index_manager.get_members_with_inheritance(owner_t or ""):
                if m.orig_name.lower() == meth_n and m.type_name:
                    return m.type_name

        m_decl = re.search(
            rf"\b([a-zA-Z_]\w*)\s+{re.escape(ident_lower)}\b",
            method_text,
            re.IGNORECASE,
        )
        if m_decl and m_decl.group(1).lower() in self.index_manager.class_members:
            return m_decl.group(1).lower()

        return None

    def infer_rhs_type(
        self, expr_node: Node | None, source: str, current_class: str | None
    ) -> str | None:
        if not expr_node or expr_node.type not in (
            "function_expression",
            "typecast_expression",
        ):
            return None

        fn_node = expr_node.child_by_field_name(
            "function"
        ) or expr_node.child_by_field_name("type")
        if not fn_node:
            return None
        fn_name = node_text(fn_node, source).lower()

        if fn_name == "new":
            args = expr_node.children_by_field_name(
                "argument"
            ) or expr_node.children_by_field_name("target")
            for arg in args:
                raw = node_text(arg, source).strip("'").strip('"').lower()
                if self.index_manager.is_known_class_or_struct(raw):
                    return raw
        elif self.index_manager.is_known_class_or_struct(fn_name):
            return fn_name

        if fn_node.type == "member_access_expression" and fn_node.children:
            obj_node = fn_node.children[0]
            mem_node = fn_node.child_by_field_name("member")
            if obj_node and mem_node:
                obj_name = node_text(obj_node, source).lower()
                mem_name = node_text(mem_node, source).lower()
                owner_type = current_class if obj_name == "self" else obj_name
                for m in self.index_manager.get_members_with_inheritance(
                    owner_type or ""
                ):
                    if m.orig_name.lower() == mem_name and m.type_name:
                        return m.type_name

        return None

    def collect_visible_locals(
        self, source: str, point: tuple[int, int], keywords: list[str]
    ) -> list[types.CompletionItem]:
        """Собирает локальные переменные, параметры и члены текущего класса для автодополнения."""
        items: list[types.CompletionItem] = []
        seen: set[str] = set()

        tree = self.parser.parse(bytes(source, "utf-8"))
        current_method, owner_node = self.find_enclosing_by_line(tree.root_node, point[0])

        # 1. Члены текущего класса и его родителей (если мы внутри класса/структуры)
        if owner_node:
            name_node = owner_node.child_by_field_name("name")
            if name_node:
                cls_lower = node_text(name_node, source).lower()
                for m in self.index_manager.get_members_with_inheritance(cls_lower):
                    key = m.orig_name.lower()
                    if key not in seen:
                        seen.add(key)
                        sig_lines = m.signature.splitlines()
                        sig = (
                            sig_lines[1].strip() if len(sig_lines) > 1 else m.signature
                        )
                        items.append(
                            types.CompletionItem(
                                label=m.orig_name,
                                kind=m.kind,
                                detail=sig,
                            )
                        )

        if not current_method:
            return items

        # 2. Параметры текущего метода
        param_list = next(
            (c for c in current_method.children if c.type == "parameter_list"), None
        )
        if param_list:
            p_types = param_list.children_by_field_name("type")
            p_names = param_list.children_by_field_name("name")
            for t_node, n_node in zip(p_types, p_names):
                orig = node_text(n_node, source)
                key = orig.lower()
                if key not in seen:
                    seen.add(key)
                    t_str = node_text(t_node, source)
                    items.append(
                        types.CompletionItem(
                            label=orig,
                            kind=types.CompletionItemKind.Variable,
                            detail=f"{t_str} {orig}",
                        )
                    )

        # 3. Локальные переменные из AST метода (до текущей строки)
        def walk_method(n: Node) -> None:
            if n.start_point.row > point[0]:
                return
            if n.type == "declaration_statement":
                t_node = n.child_by_field_name("type")
                t_str = node_text(t_node, source) if t_node else "let"
                for child in n.children:
                    if child.type == "identifier":
                        orig = node_text(child, source)
                        key = orig.lower()
                        if key not in seen:
                            seen.add(key)
                            items.append(
                                types.CompletionItem(
                                    label=orig,
                                    kind=types.CompletionItemKind.Variable,
                                    detail=f"{t_str} {orig}",
                                )
                            )
                    elif child.type == "assignment_expression":
                        left = child.child_by_field_name("left")
                        if left and left.type == "identifier":
                            orig = node_text(left, source)
                            key = orig.lower()
                            if key not in seen:
                                seen.add(key)
                                items.append(
                                    types.CompletionItem(
                                        label=orig,
                                        kind=types.CompletionItemKind.Variable,
                                        detail=f"{t_str} {orig}",
                                    )
                                )
            elif n.type == "foreach_statement":
                for f_name in ("left", "value"):
                    f_node = n.child_by_field_name(f_name)
                    if f_node and f_node.type == "identifier":
                        orig = node_text(f_node, source)
                        key = orig.lower()
                        if key not in seen:
                            seen.add(key)
                            items.append(
                                types.CompletionItem(
                                    label=orig,
                                    kind=types.CompletionItemKind.Variable,
                                    detail=orig,
                                )
                            )
            for ch in n.children:
                walk_method(ch)

        walk_method(current_method)

        # 4. Резервный поиск по тексту строк метода до курсора (на случай ERROR при наборе)
        method_lines = source.splitlines()[
            current_method.start_point.row : point[0] + 1
        ]
        method_prefix = "\n".join(method_lines)
        for mm in re.finditer(
            r"\b(?:let|[a-zA-Z_]\w*(?:<[^>]+>)?)\s+([a-zA-Z_]\w*)\s*(?:=|;|\[|,)",
            method_prefix,
        ):
            orig = mm.group(1)
            key = orig.lower()
            if key not in seen and key not in keywords:
                seen.add(key)
                items.append(
                    types.CompletionItem(
                        label=orig,
                        kind=types.CompletionItemKind.Variable,
                        detail=orig,
                    )
                )

        return items
