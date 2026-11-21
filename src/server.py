import re
import zipfile
from pathlib import Path

import tree_sitter_zscript as tszscript
from lsprotocol import types
from pygls.lsp.server import LanguageServer
from tree_sitter import Language, Node, Parser

from index_manager import IndexManager
from semantic_validator import SemanticValidator
from symbol_info import SymbolInfo
from utils import (
    node_text,
    node_to_range,
    resolve_include_path,
    uri_to_path,
)

ZSCRIPT_LANGUAGE = Language(tszscript.language())
parser: Parser = Parser(ZSCRIPT_LANGUAGE)

server = LanguageServer("zscript-lsp", "0.1.1")

index_manager: IndexManager


keywords: list[str]
builtin_identifiers: list[str]
indexed_pk3s: set[str] = set()

TOKEN_TYPES = [
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

TOKEN_MODIFIERS = [
    "declaration",  # 1 << 0 (1)
    "static",  # 1 << 1 (2)
    "readonly",  # 1 << 2 (4)
]

TOKEN_TYPE_INDEX = {name: i for i, name in enumerate(TOKEN_TYPES)}


def apply_settings(raw_settings: object, ls: LanguageServer) -> None:
    if not isinstance(raw_settings, dict):
        return
    # Zed может передать настройки напрямую или вложенными в ключ "zscript-lsp"
    cfg = raw_settings.get("zscript-lsp", raw_settings)
    pk3_list = cfg.get("pk3_paths", []) if isinstance(cfg, dict) else []
    if not pk3_list:
        ls.window_show_message(
            types.ShowMessageParams(types.MessageType.Warning, "PK3 List is empty")
        )
    loaded_count = 0
    for pk3 in pk3_list:
        if isinstance(pk3, str):
            try:
                index_manager.index_pk3(pk3)
                loaded_count += 1
            except zipfile.BadZipfile as err:
                ls.window_show_message(
                    types.ShowMessageParams(types.MessageType.Error, str(err))
                )
            except OSError as err:
                ls.window_show_message(
                    types.ShowMessageParams(types.MessageType.Error, str(err))
                )

    ls.window_show_message(
        types.ShowMessageParams(
            types.MessageType.Info,
            f"{loaded_count} pk3(s) loaded.",
        )
    )


def load_object_methods() -> None:
    builtin_object_methods = [
        (
            "GetClass",
            "class GetClass() const;",
            "class",
            types.CompletionItemKind.Method,
        ),
        (
            "GetClassName",
            "name GetClassName() const;",
            "name",
            types.CompletionItemKind.Method,
        ),
        (
            "GetParentClass",
            "class GetParentClass() const;",
            "class",
            types.CompletionItemKind.Method,
        ),
        ("Destroy", "virtual void Destroy();", "void", types.CompletionItemKind.Method),
        (
            "bDestroyed",
            "readonly bool bDestroyed;",
            "bool",
            types.CompletionItemKind.Property,
        ),
    ]
    existing_obj = {
        m.orig_name.lower() for m in index_manager.class_members.get("object", [])
    }
    for m_name, m_sig, m_type, m_kind in builtin_object_methods:
        if m_name.lower() not in existing_obj:
            index_manager.class_members.setdefault("object", []).append(
                SymbolInfo(
                    orig_name=m_name,
                    kind=m_kind,
                    uri="builtin://object",
                    location=types.Location(
                        uri="builtin://object",
                        range=types.Range(
                            start=types.Position(line=0, character=0),
                            end=types.Position(line=0, character=0),
                        ),
                    ),
                    signature=f"class Object {{\n    {m_sig}\n}}",
                    owner="Object",
                    type_name=m_type,
                )
            )


def register_builtin_zscript_members() -> None:
    """Регистрирует встроенные системные свойства движка ZScript, отсутствующие в .zs."""
    builtin_loc = types.Location(
        uri="builtin://zscript",
        range=types.Range(
            start=types.Position(line=0, character=0),
            end=types.Position(line=0, character=0),
        ),
    )
    default_prop = SymbolInfo(
        orig_name="Default",
        kind=types.CompletionItemKind.Property,
        uri="builtin://zscript",
        location=builtin_loc,
        signature="readonly<Actor> Default",
        owner="actor",
        type_name="actor",
    )
    existing = [
        m.orig_name.lower() for m in index_manager.class_members.get("actor", [])
    ]
    if "default" not in existing:
        index_manager.class_members.setdefault("actor", []).append(default_prop)
        index_manager.symbol_index.setdefault("default", []).append(default_prop)


def load_actors_builtin_actor_flags() -> None:
    flags_path = Path(__file__).parent / "builtin_actor_flags.txt"
    if flags_path.is_file():
        dummy_loc = types.Location(
            uri=flags_path.as_uri(),
            range=types.Range(
                start=types.Position(line=0, character=0),
                end=types.Position(line=0, character=0),
            ),
        )
        for line in flags_path.read_text(encoding="utf-8").splitlines():
            flag_name = line.strip()
            if not flag_name:
                continue
            # Красивый регистр: bDropped (если в файле в нижнем регистре)
            orig = (
                f"b{flag_name[1:].capitalize()}"
                if flag_name.startswith("b")
                else flag_name
            )
            info = SymbolInfo(
                orig_name=orig,
                kind=types.CompletionItemKind.Property,
                uri=flags_path.as_uri(),
                location=dummy_loc,
                signature=f"bool {orig}; // Built-in Actor flag",
                owner="Actor",
                type_name="bool",
            )
            index_manager.class_members.setdefault("actor", []).append(info)


def build_semantic_tokens(source: str) -> list[int]:
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


def get_workspace_root_for_uri(ls: LanguageServer, uri: str) -> Path | None:
    file_path = uri_to_path(uri).resolve()
    for folder in ls.workspace.folders.values():
        root = uri_to_path(folder.uri).resolve()
        if root == file_path or root in file_path.parents:
            return root
    for folder in ls.workspace.folders.values():
        return uri_to_path(folder.uri).resolve()
    return file_path.parent


def validate_and_index(ls: LanguageServer, uri: str, source: str) -> None:
    root = get_workspace_root_for_uri(ls, uri)
    diagnostics = index_manager.index_source(uri, source, workspace_root=root)
    validator = SemanticValidator(index_manager, builtin_identifiers)
    diagnostics.extend(validator.validate(source))
    ls.text_document_publish_diagnostics(
        types.PublishDiagnosticsParams(uri=uri, diagnostics=diagnostics)
    )


def collect_visible_locals(
    source: str, point: tuple[int, int]
) -> list[types.CompletionItem]:
    """Собирает локальные переменные, параметры и члены текущего класса для автодополнения."""
    items: list[types.CompletionItem] = []
    seen: set[str] = set()

    tree = parser.parse(bytes(source, "utf-8"))
    current_method, owner_node = find_enclosing_by_line(tree.root_node, point[0])

    # 1. Члены текущего класса и его родителей (если мы внутри класса/структуры)
    if owner_node:
        name_node = owner_node.child_by_field_name("name")
        if name_node:
            cls_lower = node_text(name_node, source).lower()
            for m in get_members_with_inheritance(cls_lower):
                key = m.orig_name.lower()
                if key not in seen:
                    seen.add(key)
                    sig_lines = m.signature.splitlines()
                    sig = sig_lines[1].strip() if len(sig_lines) > 1 else m.signature
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
    method_lines = source.splitlines()[current_method.start_point.row : point[0] + 1]
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


def get_members_with_inheritance(class_key: str) -> list[SymbolInfo]:
    result: list[SymbolInfo] = []
    seen_members: set[str] = set()
    visited_classes: set[str] = set()

    curr = class_key.lower()
    while curr and curr not in visited_classes:
        visited_classes.add(curr)
        for member in index_manager.class_members.get(curr, []):
            m_key = member.orig_name.lower()
            if m_key not in seen_members:
                seen_members.add(m_key)
                result.append(member)
        next_parent = index_manager.class_parents.get(curr, "")
        if not next_parent and curr != "object" and "object" not in visited_classes:
            next_parent = "object"
        curr = next_parent

    return result


def infer_rhs_type(
    expr_node: Node, source: str, current_class: str | None
) -> str | None:
    """Detects right side for an assignment (e.g for let x = ...)."""

    if expr_node.type not in ("function_expression", "typecast_expression"):
        return None

    fn_node = expr_node.child_by_field_name(
        "function"
    ) or expr_node.child_by_field_name("type")
    if not fn_node:
        return None
    fn_name = node_text(fn_node, source).lower()

    # 1. new("MyLibrary") or MyLibrary(arg)
    if fn_name == "new":
        args = expr_node.children_by_field_name(
            "argument"
        ) or expr_node.children_by_field_name("target")
        for arg in args:
            raw = node_text(arg, source).strip("'").strip('"').lower()
            if raw in index_manager.class_members or raw in index_manager.symbol_index:
                return raw
    elif (
        fn_name in index_manager.class_members or fn_name in index_manager.symbol_index
    ):
        return fn_name

    # 2. Method Call: MyLibrary.GetInstance() или obj.GetItem()
    if fn_node.type == "member_access_expression":
        obj_node = fn_node.children[0] if fn_node.children else None
        mem_node = fn_node.child_by_field_name("member")
        if obj_node and mem_node:
            obj_name = node_text(obj_node, source).lower()
            mem_name = node_text(mem_node, source).lower()
            owner_type = current_class if obj_name == "self" else obj_name
            for m in get_members_with_inheritance(owner_type or ""):
                if m.orig_name.lower() == mem_name and m.type_name:
                    return m.type_name

    return None


def find_enclosing_by_line(root: Node, line: int) -> tuple[Node | None, Node | None]:
    """Finds the nearest method and class/struct based on the line number (resilient to ERROR nodes in the input)."""
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


def resolve_identifier_type(
    ident_lower: str,
    source: str,
    point: tuple[int, int],
) -> str | None:
    """Returns the name of the class/struct (in lowercase) for the identifier at the cursor position."""
    # 1. The identifier itself is a known class/struct (MyLibrary.)
    if (
        ident_lower in index_manager.class_members
        or ident_lower in index_manager.symbol_index
    ):
        for entry in index_manager.get_symbol(ident_lower) or []:
            if entry.kind in (
                types.CompletionItemKind.Class,
                types.CompletionItemKind.Struct,
            ):
                return ident_lower

    tree = parser.parse(bytes(source, "utf-8"))
    # Find the current method and the current class/struct surrounding the cursor
    current_method, owner_node = find_enclosing_by_line(tree.root_node, point[0])

    current_class: str | None = None

    if owner_node:
        name_node = owner_node.child_by_field_name("name")
        if name_node:
            current_class = node_text(name_node, source).lower()

    # 2. Process self and super
    if ident_lower == "self":
        return current_class
    if ident_lower == "super" and current_class:
        return index_manager.class_parents.get(current_class)

    # 3. Searching among the parameters and local variables of the current method
    if current_method:
        # 3a. Method parameters
        param_list = next(
            (c for c in current_method.children if c.type == "parameter_list"), None
        )
        if param_list:
            p_types = param_list.children_by_field_name("type")
            p_names = param_list.children_by_field_name("name")
            for t_node, n_node in zip(p_types, p_names):
                if node_text(n_node, source).lower() == ident_lower:
                    return node_text(t_node, source).strip("@").lower()

        # 3b. Local variables inside the method (including 'let')
        def scan_locals(n: Node) -> str | None:
            if n.type == "declaration_statement":
                t_node = n.child_by_field_name("type")
                if t_node:
                    t_str = node_text(t_node, source).strip("@").lower()
                    for child in n.children:
                        if (
                            child.type == "identifier"
                            and node_text(child, source).lower() == ident_lower
                        ):
                            return t_str
                        if child.type == "assignment_expression":
                            left = child.child_by_field_name("left")
                            if left and node_text(left, source).lower() == ident_lower:
                                return t_str
                else:
                    # let x = ...;
                    for child in n.children:
                        if child.type == "assignment_expression":
                            left = child.child_by_field_name("left")
                            right = child.child_by_field_name("right")
                            if not left or not right:
                                continue
                            if node_text(left, source).lower() == ident_lower:
                                return infer_rhs_type(right, source, current_class)

            for ch in n.children:
                res = scan_locals(ch)
                if res:
                    return res
            return None

        local_type = scan_locals(current_method)
        if not local_type and current_method:
            method_text = node_text(current_method, source)
            # Ищем "let shotgunner = new("ShotgunGuy")" или "ShotgunGuy shotgunner"
            m_new = re.search(
                rf"\blet\s+{re.escape(ident_lower)}\s*=\s*new\s*\(\s*['\"]([^'\"]+)['\"]\s*\)",
                method_text,
                re.IGNORECASE,
            )
            if m_new:
                return m_new.group(1).lower()

            m_call = re.search(
                rf"\blet\s+{re.escape(ident_lower)}\s*=\s*([a-zA-Z_]\w*)\.([a-zA-Z_]\w*)\s*\(",
                method_text,
                re.IGNORECASE,
            )
            if m_call:
                cls_n, meth_n = m_call.group(1).lower(), m_call.group(2).lower()
                owner_t = current_class if cls_n == "self" else cls_n
                for m in get_members_with_inheritance(owner_t or ""):
                    if m.orig_name.lower() == meth_n and m.type_name:
                        return m.type_name

            m_decl = re.search(
                rf"\b([a-zA-Z_]\w*)\s+{re.escape(ident_lower)}\b",
                method_text,
                re.IGNORECASE,
            )
            if m_decl and m_decl.group(1).lower() in index_manager.class_members:
                return m_decl.group(1).lower()

        if local_type:
            return local_type

    # 4. Searching among the fields of the current class and its parents
    if current_class:
        for member in get_members_with_inheritance(current_class):
            if member.orig_name.lower() == ident_lower and member.type_name:
                return member.type_name

    return None


@server.feature(types.INITIALIZE)
def on_initialize(ls: LanguageServer, params: types.InitializeParams) -> None:
    if params.initialization_options:
        apply_settings(params.initialization_options, ls)


@server.feature(types.INITIALIZED)
def on_initialized(ls: LanguageServer, params: types.InitializedParams) -> None:
    try:
        global keywords
        keywords_path = Path(__file__).parent / "keywords.txt"
        keywords = [
            line.lower()
            for line in keywords_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

        global builtin_identifiers
        identifiers_path = Path(__file__).parent / "identifiers.txt"
        builtin_identifiers = [
            line.lower()
            for line in identifiers_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        load_object_methods()
        register_builtin_zscript_members()
        load_actors_builtin_actor_flags()
    except OSError as err:
        ls.window_show_message(
            types.ShowMessageParams(types.MessageType.Error, str(err))
        )

    visited_uris: set[str] = set()
    for folder in ls.workspace.folders.values():
        root_path = uri_to_path(folder.uri).resolve()
        if not root_path.exists():
            continue

        # Check for root file
        for root_entry in (
            "zscript",
            "zscript.zs",
            "zscript.zsc",
            "zscript.zscript",
            "zscript.txt",
        ):
            entry_file = root_path / root_entry
            if entry_file.is_file():
                entry_uri = entry_file.as_uri()
                if entry_uri not in visited_uris:
                    try:
                        text = entry_file.read_text(encoding="utf-8", errors="ignore")
                        index_manager.index_source(
                            uri=entry_uri,
                            source=text,
                            workspace_root=root_path,
                            visited_uris=visited_uris,
                        )
                    except OSError:
                        pass
        # Check others
        for ext in ("*.zs", "*.zsc", "*.zscript"):
            for file_path in root_path.rglob(ext):
                file_uri = file_path.as_uri()
                if file_uri not in visited_uris:
                    try:
                        text = file_path.read_text(encoding="utf-8", errors="ignore")
                        index_manager.index_source(
                            uri=file_uri,
                            source=text,
                            workspace_root=root_path,
                            visited_uris=visited_uris,
                        )
                    except OSError:
                        pass


@server.feature(types.TEXT_DOCUMENT_DID_OPEN)
def did_open(ls: LanguageServer, params: types.DidOpenTextDocumentParams) -> None:
    doc = ls.workspace.get_text_document(params.text_document.uri)
    validate_and_index(ls, doc.uri, doc.source)


@server.feature(types.TEXT_DOCUMENT_DID_CHANGE)
def did_change(ls: LanguageServer, params: types.DidChangeTextDocumentParams) -> None:
    doc = ls.workspace.get_text_document(params.text_document.uri)
    validate_and_index(ls, doc.uri, doc.source)


@server.feature(
    types.TEXT_DOCUMENT_COMPLETION, types.CompletionOptions(trigger_characters=["."])
)
def completions(
    ls: LanguageServer, params: types.CompletionParams
) -> types.CompletionList:
    items = []

    doc = ls.workspace.get_text_document(params.text_document.uri)
    current_line = doc.lines[params.position.line][: params.position.character]

    if "." in current_line:
        before_dot = current_line.rsplit(".", 1)[0].rstrip()
        ident_chars = []
        for ch in reversed(before_dot):
            if ch.isalnum() or ch == "_":
                ident_chars.append(ch)
            else:
                break
        owner_ident = "".join(reversed(ident_chars)).lower()
        if owner_ident:
            point = (params.position.line, params.position.character)
            resolved_type = resolve_identifier_type(owner_ident, doc.source, point)
            target_class = resolved_type or owner_ident
            members = get_members_with_inheritance(target_class)
            for m in members:
                sig_lines = m.signature.splitlines()
                signature = sig_lines[1].strip() if len(sig_lines) > 1 else m.signature
                items.append(
                    types.CompletionItem(
                        label=m.orig_name,
                        kind=m.kind,
                        detail=signature,
                    )
                )
    else:
        point = (params.position.line, params.position.character)
        items.extend(collect_visible_locals(doc.source, point))

        for kw in keywords:
            items.append(
                types.CompletionItem(label=kw, kind=types.CompletionItemKind.Keyword)
            )
        allowed_global_kinds = {
            types.CompletionItemKind.Class,
            types.CompletionItemKind.Struct,
            types.CompletionItemKind.Enum,
        }
        for entries in index_manager.symbol_index.values():
            for entry in entries:
                if entry.kind in allowed_global_kinds:
                    items.append(
                        types.CompletionItem(
                            label=entry.orig_name,
                            kind=entry.kind,
                            detail=entry.signature,
                        )
                    )
                    break

    return types.CompletionList(is_incomplete=False, items=items)


@server.feature(types.TEXT_DOCUMENT_HOVER)
def hover(ls: LanguageServer, params: types.HoverParams) -> types.Hover | None:
    doc = ls.workspace.get_text_document(params.text_document.uri)
    tree = parser.parse(bytes(doc.source, "utf-8"))
    point = (params.position.line, params.position.character)
    node = tree.root_node.descendant_for_point_range(point, point)
    if not node:
        return None

    target_key = node_text(node, doc.source).strip('"').strip("'").lower()

    filtered_entries: list[SymbolInfo] | None = None
    parent = node.parent

    # 1. Вызов через точку: obj.Method() или ClassName.Method()
    if (
        parent
        and parent.type == "member_access_expression"
        and (member_node := parent.child_by_field_name("member"))
        and member_node.id == node.id
        and parent.children
    ):
        obj_node = parent.children[0]
        obj_ident = node_text(obj_node, doc.source).lower()
        resolved_owner = resolve_identifier_type(obj_ident, doc.source, point)
        if resolved_owner:
            members = get_members_with_inheritance(resolved_owner)
            matched = [m for m in members if m.orig_name.lower() == target_key]
            if matched:
                filtered_entries = matched

    # 2. Вызов без точки внутри класса: DrawBarKeys()
    if filtered_entries is None:
        _, owner_node = find_enclosing_by_line(tree.root_node, point[0])
        if owner_node:
            name_node = owner_node.child_by_field_name("name")
            if name_node:
                current_class = node_text(name_node, doc.source).lower()
                # get_members_with_inheritance идёт от потомка к родителю,
                # поэтому первый найденный элемент — это метод текущего класса
                members = get_members_with_inheritance(current_class)
                matched = [m for m in members if m.orig_name.lower() == target_key]
                if matched:
                    filtered_entries = [matched[0]]

    # 3. Откат на глобальный индекс (имена классов, глобальные константы и т.д.)
    entries = (
        filtered_entries
        if filtered_entries is not None
        else index_manager.get_symbol(target_key)
    )
    if not entries:
        return None

    unique_sigs = list(dict.fromkeys(e.signature for e in entries))
    markdown = "\n\n---\n\n".join(f"```zscript\n{sig}\n```" for sig in unique_sigs)

    return types.Hover(
        contents=types.MarkupContent(
            kind=types.MarkupKind.Markdown,
            value=markdown,
        ),
        range=node_to_range(node),
    )


@server.feature(types.TEXT_DOCUMENT_DEFINITION)
def goto_definition(
    ls: LanguageServer, params: types.DefinitionParams
) -> list[types.Location] | types.Location | None:
    doc = ls.workspace.get_text_document(params.text_document.uri)
    tree = parser.parse(bytes(doc.source, "utf-8"))
    point = (params.position.line, params.position.character)
    node = tree.root_node.descendant_for_point_range(point, point)
    if not node:
        return None

    # 1. Переход по #include "path/to/file.zs"
    curr: Node | None = node
    while curr:
        if curr.type == "include_definition":
            path_node = curr.child_by_field_name("path")
            if path_node:
                inc_text = node_text(path_node, doc.source)
                root = get_workspace_root_for_uri(ls, doc.uri)
                target_file = resolve_include_path(root, doc.uri, inc_text)
                if target_file:
                    return types.Location(
                        uri=target_file.as_uri(),
                        range=types.Range(
                            start=types.Position(line=0, character=0),
                            end=types.Position(line=0, character=0),
                        ),
                    )
            return None
        curr = curr.parent

    target_key = node_text(node, doc.source).strip('"').strip("'").lower()
    if not target_key:
        return None

    # 2. Точный переход для члена класса после точки (например: lib.Print или MyLibrary.GetInstance)
    parent = node.parent
    if parent and parent.type == "member_access_expression":
        member_node = parent.child_by_field_name("member")
        if member_node and member_node.id == node.id and parent.children:
            obj_node = parent.children[0]
            obj_ident = node_text(obj_node, doc.source).lower()
            resolved_owner = resolve_identifier_type(obj_ident, doc.source, point)
            if resolved_owner:
                members = get_members_with_inheritance(resolved_owner)
                matched = [
                    m.location for m in members if m.orig_name.lower() == target_key
                ]
                if matched:
                    return matched

    # 3. Поиск среди параметров и локальных переменных текущего метода
    current_method, _ = find_enclosing_by_line(tree.root_node, point[0])
    if current_method:
        # 3a. Параметры метода
        param_list = next(
            (c for c in current_method.children if c.type == "parameter_list"), None
        )
        if param_list:
            for n_node in param_list.children_by_field_name("name"):
                if node_text(n_node, doc.source).lower() == target_key:
                    return types.Location(uri=doc.uri, range=node_to_range(n_node))

        # 3b. Локальные переменные и константы внутри метода
        found_local: Node | None = None

        def find_local_decl(n: Node) -> None:
            nonlocal found_local
            if found_local or n.start_point.row > point[0]:
                return

            if n.type == "declaration_statement":
                for child in n.children:
                    if (
                        child.type == "identifier"
                        and node_text(child, doc.source).lower() == target_key
                    ):
                        found_local = child
                        return
                    if child.type == "assignment_expression":
                        left = child.child_by_field_name("left")
                        if (
                            left
                            and left.type == "identifier"
                            and node_text(left, doc.source).lower() == target_key
                        ):
                            found_local = left
                            return

            elif n.type in ("const_definition", "const_array_definition"):
                name_node = n.child_by_field_name("name")
                if name_node and node_text(name_node, doc.source).lower() == target_key:
                    found_local = name_node
                    return

            elif n.type == "foreach_statement":
                for f_name in ("left", "value"):
                    f_node = n.child_by_field_name(f_name)
                    if (
                        f_node
                        and f_node.type == "identifier"
                        and node_text(f_node, doc.source).lower() == target_key
                    ):
                        found_local = f_node
                        return

            for ch in n.children:
                find_local_decl(ch)

        find_local_decl(current_method)
        if found_local:
            return types.Location(uri=doc.uri, range=node_to_range(found_local))

    # 4. Переход к глобальным классам, структурам, полям и методам
    entries = index_manager.get_symbol(target_key)
    if not entries:
        return None
    return [e.location for e in entries]


@server.feature(
    types.TEXT_DOCUMENT_SEMANTIC_TOKENS_FULL,
    types.SemanticTokensLegend(
        token_types=TOKEN_TYPES,
        token_modifiers=TOKEN_MODIFIERS,
    ),
)
def semantic_tokens_full(
    ls: LanguageServer, params: types.SemanticTokensParams
) -> types.SemanticTokens:
    doc = ls.workspace.get_text_document(params.text_document.uri)
    data = build_semantic_tokens(doc.source)
    return types.SemanticTokens(data=data)


@server.feature(types.WORKSPACE_DID_CHANGE_CONFIGURATION)
def did_change_configuration(
    ls: LanguageServer, params: types.DidChangeConfigurationParams
) -> None:
    if params.settings:
        apply_settings(params.settings, ls)


if __name__ == "__main__":
    index_manager = IndexManager(parser=parser)
    server.start_io()
