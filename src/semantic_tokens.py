from lsprotocol import types
from tree_sitter import Node, Parser

from index_manager import IndexManager
from utils import node_text

TOKEN_TYPES: list[str] = [
    "class",  # 0
    "struct",  # 1
    "enum",  # 2
    "enumMember",  # 3
    "function",  # 4
    "method",  # 5
    "parameter",  # 6
    "variable",  # 7
    "property",  # 8
    "keyword",  # 9
    "type",  # 10
    "number",  # 11
    "string",  # 12
    "comment",  # 13
    "operator",  # 14
]

TOKEN_MODIFIERS: list[str] = [
    "declaration",  # 1 << 0 (1)
    "static",  # 1 << 1 (2)
    "readonly",  # 1 << 2 (4)
]

TOKEN_TYPE_INDEX = {name: i for i, name in enumerate(TOKEN_TYPES)}


def build_semantic_tokens(parser: Parser, index_manager: IndexManager, source: str) -> list[int]:
    tree = parser.parse(bytes(source, "utf-8"))
    # Собираем сырые токены: (line, start_char, length, type_idx, modifiers_mask)
    raw_tokens: list[tuple[int, int, int, int, int]] = []

    def add_tok(node: Node | None, tok_type: str, mod_mask: int = 0) -> None:
        if not node:
            return
        # Однострочные токены (если многострочный комментарий/строка — разбиваем построчно)
        start_row, start_col = node.start_point
        end_row, end_col = node.end_point
        t_idx = TOKEN_TYPE_INDEX[tok_type]

        if start_row == end_row:
            length = end_col - start_col
            if length > 0:
                raw_tokens.append((start_row, start_col, length, t_idx, mod_mask))
        else:
            lines = source.splitlines()
            for r in range(start_row, min(end_row + 1, len(lines))):
                c_start = start_col if r == start_row else 0
                c_end = end_col if r == end_row else len(lines[r])
                if c_end > c_start:
                    raw_tokens.append((r, c_start, c_end - c_start, t_idx, mod_mask))

    def walk(node: Node) -> None:
        ntype = node.type

        if ntype == "comment":
            add_tok(node, "comment")
            return
        elif ntype in ("string_literal", "name_literal"):
            add_tok(node, "string")
            return
        elif ntype == "number_literal":
            add_tok(node, "number")
            return
        elif ntype == "predefined_type":
            add_tok(node, "type")
            return
        elif ntype in ("scope", "modifier", "access_level"):
            add_tok(node, "keyword")
            return
        elif ntype == "class_definition":
            add_tok(node.child_by_field_name("name"), "class", 1)
            inherit = node.child_by_field_name("inherit")
            if inherit and inherit.next_named_sibling:
                add_tok(inherit.next_named_sibling, "class")
        elif ntype == "struct_definition":
            add_tok(node.child_by_field_name("name"), "struct", 1)
        elif ntype == "enum_declaration":
            add_tok(node.child_by_field_name("name"), "enum", 1)
        elif ntype == "enum_declaration_value":
            add_tok(node.child_by_field_name("name"), "enumMember", 5)
        elif ntype == "method_declaration":
            add_tok(node.child_by_field_name("name"), "method", 1)
        elif ntype == "const_definition":
            add_tok(node.child_by_field_name("name"), "variable", 5)
        elif ntype == "state_label":
            add_tok(node.child_by_field_name("label"), "function", 1)
        elif ntype == "class_type":
            add_tok(node, "class")
        elif ntype == "parameter_list":
            for p_name in node.children_by_field_name("name"):
                add_tok(p_name, "parameter", 1)
        elif ntype == "function_expression":
            fn = node.child_by_field_name("function")
            if fn and fn.type == "identifier":
                add_tok(fn, "function")
            elif fn and fn.type == "member_access_expression":
                mem = fn.child_by_field_name("member")
                if mem and mem.type == "identifier":
                    add_tok(mem, "method")
        elif ntype == "member_access_expression":
            # Если это доступ к полю (не вызов метода)
            if not (node.parent and node.parent.type == "function_expression"):
                mem = node.child_by_field_name("member")
                if mem and mem.type == "identifier":
                    add_tok(mem, "property")
        elif ntype == "identifier":
            # Проверяем, не является ли идентификатор известным классом/структурой/enum из индекса
            parent_type = node.parent.type if node.parent else ""
            if parent_type not in (
                "class_definition",
                "struct_definition",
                "enum_declaration",
                "enum_declaration_value",
                "method_declaration",
                "const_definition",
                "state_label",
                "class_type",
                "parameter_list",
                "function_expression",
                "member_access_expression",
                "mixin_statement",
            ):
                ident_lower = node_text(node, source).lower()
                entries = index_manager.get_symbol(ident_lower)
                if entries:
                    kind = entries[0].kind
                    if kind == types.CompletionItemKind.Class:
                        add_tok(node, "class")
                    elif kind == types.CompletionItemKind.Struct:
                        add_tok(node, "struct")
                    elif kind == types.CompletionItemKind.Enum:
                        add_tok(node, "enum")
                    elif kind == types.CompletionItemKind.EnumMember:
                        add_tok(node, "enumMember")
                    elif kind == types.CompletionItemKind.Constant:
                        add_tok(node, "variable", 4)
                else:
                    add_tok(node, "variable")

        for child in node.children:
            walk(child)

    walk(tree.root_node)

    # Сортируем токены строго по позиции в файле (требование спецификации LSP)
    raw_tokens.sort(key=lambda t: (t[0], t[1]))

    # Кодируем в относительные смещения (delta_line, delta_start, length, type, modifiers)
    encoded: list[int] = []
    prev_line = 0
    prev_start = 0
    for line, start_char, length, t_idx, mod_mask in raw_tokens:
        delta_line = line - prev_line
        delta_start = start_char if delta_line > 0 else start_char - prev_start
        encoded.extend([delta_line, delta_start, length, t_idx, mod_mask])
        prev_line = line
        prev_start = start_char

    return encoded
