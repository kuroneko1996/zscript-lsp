from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlparse

import tree_sitter_zscript as tszscript
from lsprotocol import types
from pygls.lsp.server import LanguageServer
from tree_sitter import Language, Node, Parser

ZSCRIPT_LANGUAGE = Language(tszscript.language())
parser = Parser(ZSCRIPT_LANGUAGE)

server = LanguageServer("zscript-lsp", "0.1.0")

keywords: list[str]
builtin_identifiers: list[str]


@dataclass
class SymbolInfo:
    orig_name: str
    kind: types.CompletionItemKind
    uri: str
    location: types.Location
    signature: str
    owner: str | None = None
    type_name: str | None = None


# Global index: { "lower_case_name": [SymbolInfo, ...] }
symbol_index: dict[str, list[SymbolInfo]] = {}
# Class index: { "lower_case_class_name": [SymbolInfo, ...] }
class_members: dict[str, list[SymbolInfo]] = {}
# Inheritance {"child_class_lower_case": "parent_class_lower_case"}
class_parents: dict[str, str] = {}

# Links symbols to files for quick clearing or reindexing
file_symbols: dict[str, set[str]] = {}
file_classes: dict[str, set[str]] = {}


def uri_to_path(uri: str) -> Path:
    parsed = urlparse(uri)
    path_str = unquote(parsed.path)
    # Remove first slash symbol on Windows
    if len(path_str) >= 3 and path_str[0] == "/" and path_str[2] == ":":
        path_str = path_str[1:]
    return Path(path_str)


def node_to_range(node: Node) -> types.Range:
    return types.Range(
        start=types.Position(
            line=node.start_point.row, character=node.start_point.column
        ),
        end=types.Position(line=node.end_point.row, character=node.end_point.column),
    )


def get_workspace_root_for_uri(ls: LanguageServer, uri: str) -> Path | None:
    file_path = uri_to_path(uri).resolve()
    for folder in ls.workspace.folders.values():
        root = uri_to_path(folder.uri).resolve()
        if root == file_path or root in file_path.parents:
            return root
    for folder in ls.workspace.folders.values():
        return uri_to_path(folder.uri).resolve()
    return file_path.parent


def resolve_include_path(
    root_path: Path | None, current_uri: str, inc_str: str
) -> Path | None:
    clean_rel = inc_str.strip('"').strip("'").replace("\\", "/").strip()
    if not clean_rel:
        return None

    candidates: list[Path] = []

    if root_path:
        candidates.append((root_path / clean_rel).resolve())
    if current_uri.startswith("file://"):
        candidates.append((uri_to_path(current_uri).parent / clean_rel).resolve())

    for cand in candidates:
        if cand.is_file():
            return cand
    return None


def find_owner_node(node: Node) -> tuple[str, str] | None:
    """Returns (owner_type, original_owner_name) for a method or property."""
    parent = node.parent
    while parent:
        if parent.type in ("class_definition", "struct_definition"):
            kind = "class" if parent.type == "class_definition" else "struct"
            name_node = parent.child_by_field_name("name")
            if name_node and name_node.text:
                return kind, name_node.text.decode("utf-8")
        parent = parent.parent
    return None


def extract_signature(node: Node, source: str) -> str:
    end_byte = node.end_byte
    for child in node.children:
        if child.type in ("block", ";"):
            end_byte = child.start_byte
            break

    sig = " ".join(source[node.start_byte : end_byte].split())

    if node.type in ("method_declaration", "variable_declaration"):
        sig += ";"
        owner = find_owner_node(node)
        if owner:
            kind, owner_name = owner
            return f"{kind} {owner_name} {{\n    {sig}\n}}"
    return sig


def clear_file_index(uri: str) -> None:
    for name in file_symbols.get(uri, set()):
        if name in symbol_index:
            symbol_index[name] = [s for s in symbol_index[name] if s.uri != uri]
            if not symbol_index[name]:
                del symbol_index[name]
    file_symbols[uri] = set()

    for cls_name in file_classes.get(uri, set()):
        if cls_name in class_members:
            class_members[cls_name] = [
                m for m in class_members[cls_name] if m.uri != uri
            ]
            if not class_members[cls_name]:
                del class_members[cls_name]
    file_classes[uri] = set()


def index_source(
    uri: str,
    source: str,
    workspace_root: Path | None = None,
    visited_uris: set[str] | None = None,
) -> list[types.Diagnostic]:
    if visited_uris is None:
        visited_uris = {uri}
    else:
        visited_uris.add(uri)

    clear_file_index(uri)

    tree = parser.parse(bytes(source, "utf-8"))
    diagnostics: list[types.Diagnostic] = []

    def add_symbol(
        name_node: Node,
        decl_node: Node,
        kind: types.CompletionItemKind,
    ) -> None:
        orig_name = source[name_node.start_byte : name_node.end_byte]
        key = orig_name.lower()
        owner_info = find_owner_node(decl_node)
        owner_name = owner_info[1] if owner_info else None
        type_node = decl_node.child_by_field_name("type")
        type_name = (
            source[type_node.start_byte : type_node.end_byte].strip("@").lower()
            if type_node
            else None
        )

        info = SymbolInfo(
            orig_name=orig_name,
            kind=kind,
            uri=uri,
            location=types.Location(uri=uri, range=node_to_range(name_node)),
            signature=extract_signature(decl_node, source),
            owner=owner_name,
            type_name=type_name,
        )

        symbol_index.setdefault(key, []).append(info)
        file_symbols[uri].add(key)

        if owner_name:
            owner_key = owner_name.lower()
            class_members.setdefault(owner_key, []).append(info)
            file_classes[uri].add(owner_key)

    def walk(node: Node) -> None:
        if node.type == "ERROR" or node.is_missing:
            diagnostics.append(
                types.Diagnostic(
                    range=node_to_range(node),
                    message="ZScript syntax error",
                    severity=types.DiagnosticSeverity.Error,
                )
            )
        name_node = node.child_by_field_name("name")

        if node.type == "include_definition":
            path_node = node.child_by_field_name("path")
            if path_node:
                inc_text = source[path_node.start_byte : path_node.end_byte]
                target_file = resolve_include_path(workspace_root, uri, inc_text)
                if target_file:
                    target_uri = target_file.as_uri()
                    if target_uri not in visited_uris:
                        try:
                            inc_source = target_file.read_text(
                                encoding="utf-8", errors="ignore"
                            )
                            index_source(
                                target_uri, inc_source, workspace_root, visited_uris
                            )
                        except OSError:
                            pass
        elif name_node:
            match node.type:
                case "class_definition":
                    inherit_node = node.child_by_field_name("inherit")
                    if name_node and inherit_node:
                        cls_key = source[
                            name_node.start_byte : name_node.end_byte
                        ].lower()
                        parent_key = source[
                            inherit_node.start_byte : inherit_node.end_byte
                        ].lower()
                        class_parents[cls_key] = parent_key
                    add_symbol(name_node, node, types.CompletionItemKind.Class)
                case "struct_definition":
                    add_symbol(name_node, node, types.CompletionItemKind.Struct)
                case "enum_declaration":
                    add_symbol(name_node, node, types.CompletionItemKind.Enum)
                case "method_declaration":
                    add_symbol(name_node, node, types.CompletionItemKind.Method)
                case "variable_declaration":
                    # index only class/struct fields, not local variables
                    if find_owner_node(node):
                        for child in node.children_by_field_name("name"):
                            add_symbol(child, node, types.CompletionItemKind.Variable)
                case "const_definition" | "const_array_definition":
                    add_symbol(name_node, node, types.CompletionItemKind.Constant)
                case "enum_declaration_value":
                    add_symbol(name_node, node, types.CompletionItemKind.EnumMember)
                case "state_label":
                    add_symbol(name_node, node, types.CompletionItemKind.Event)

        for child in node.children:
            walk(child)

    walk(tree.root_node)
    return diagnostics


def validate_and_index(ls: LanguageServer, uri: str, source: str) -> None:
    root = get_workspace_root_for_uri(ls, uri)
    diagnostics = index_source(uri, source, workspace_root=root)
    diagnostics.extend(check_undefined_identifiers(source))
    ls.text_document_publish_diagnostics(
        types.PublishDiagnosticsParams(uri=uri, diagnostics=diagnostics)
    )


def get_members_with_inheritance(class_key: str) -> list[SymbolInfo]:
    result: list[SymbolInfo] = []
    seen_members: set[str] = set()
    visited_classes: set[str] = set()

    curr = class_key.lower()
    while curr and curr not in visited_classes:
        visited_classes.add(curr)
        for member in class_members.get(curr, []):
            m_key = member.orig_name.lower()
            if m_key not in seen_members:
                seen_members.add(m_key)
                result.append(member)
        curr = class_parents.get(curr, "")

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
    fn_name = source[fn_node.start_byte : fn_node.end_byte].lower()

    # 1. new("MyLibrary") or MyLibrary(arg)
    if fn_name == "new":
        args = expr_node.children_by_field_name(
            "argument"
        ) or expr_node.children_by_field_name("target")
        for arg in args:
            raw = source[arg.start_byte : arg.end_byte].strip("'").strip('"').lower()
            if raw in class_members or raw in symbol_index:
                return raw
    elif fn_name in class_members or fn_name in symbol_index:
        return fn_name

    # 2. Method Call: MyLibrary.GetInstance() или obj.GetItem()
    if fn_node.type == "member_access_expression":
        obj_node = fn_node.children[0] if fn_node.children else None
        mem_node = fn_node.child_by_field_name("member")
        if obj_node and mem_node:
            obj_name = source[obj_node.start_byte : obj_node.end_byte].lower()
            mem_name = source[mem_node.start_byte : mem_node.end_byte].lower()
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
    if ident_lower in class_members or ident_lower in symbol_index:
        for entry in symbol_index.get(ident_lower, []):
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
            current_class = source[name_node.start_byte : name_node.end_byte].lower()

    # 2. Process self and super
    if ident_lower == "self":
        return current_class
    if ident_lower == "super" and current_class:
        return class_parents.get(current_class)

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
                if source[n_node.start_byte : n_node.end_byte].lower() == ident_lower:
                    return (
                        source[t_node.start_byte : t_node.end_byte].strip("@").lower()
                    )

        # 3b. Local variables inside the method (including 'let')
        def scan_locals(n: Node) -> str | None:
            if n.type == "declaration_statement":
                t_node = n.child_by_field_name("type")
                if t_node:
                    t_str = (
                        source[t_node.start_byte : t_node.end_byte].strip("@").lower()
                    )
                    for child in n.children:
                        if (
                            child.type == "identifier"
                            and source[child.start_byte : child.end_byte].lower()
                            == ident_lower
                        ):
                            return t_str
                        if child.type == "assignment_expression":
                            left = child.child_by_field_name("left")
                            if (
                                left
                                and source[left.start_byte : left.end_byte].lower()
                                == ident_lower
                            ):
                                return t_str
                else:
                    # let x = ...;
                    for child in n.children:
                        if child.type == "assignment_expression":
                            left = child.child_by_field_name("left")
                            right = child.child_by_field_name("right")
                            if not left or not right:
                                continue
                            if (
                                source[left.start_byte : left.end_byte].lower()
                                == ident_lower
                            ):
                                return infer_rhs_type(right, source, current_class)

            for ch in n.children:
                res = scan_locals(ch)
                if res:
                    return res
            return None

        local_type = scan_locals(current_method)
        if local_type:
            return local_type

    # 4. Searching among the fields of the current class and its parents
    if current_class:
        for member in get_members_with_inheritance(current_class):
            if member.orig_name.lower() == ident_lower and member.type_name:
                return member.type_name

    return None


# region Declared Variables Check
def collect_declared_names(method_node: Node, source: str) -> set[str]:
    """Collects all parameters and local variables within a method."""
    declared: set[str] = set()

    # 1. Method parameters
    param_list = next(
        (c for c in method_node.children if c.type == "parameter_list"), None
    )
    if param_list:
        for n_node in param_list.children_by_field_name("name"):
            declared.add(source[n_node.start_byte : n_node.end_byte].lower())

    # 2. Local variables (including let, for, and foreach)
    def walk_decls(n: Node) -> None:
        if n.type == "declaration_statement":
            for child in n.children:
                if child.type == "identifier":
                    declared.add(source[child.start_byte : child.end_byte].lower())
                elif child.type == "assignment_expression":
                    left = child.child_by_field_name("left")
                    if left and left.type == "identifier":
                        declared.add(source[left.start_byte : left.end_byte].lower())

        elif n.type == "foreach_statement":
            for field_name in ("left", "value"):
                f_node = n.child_by_field_name(field_name)
                if f_node and f_node.type == "identifier":
                    declared.add(source[f_node.start_byte : f_node.end_byte].lower())

        for ch in n.children:
            walk_decls(ch)

    walk_decls(method_node)
    return declared


def is_ignored_identifier(node: Node) -> bool:
    """Проверяет, является ли идентификатор именем типа, поля после точки или объявлением."""
    parent = node.parent
    if not parent:
        return True

    # Правая часть после точки: obj.member
    if parent.type == "member_access_expression":
        member_node = parent.child_by_field_name("member")
        if member_node and member_node.id == node.id:
            return True

    # Объявления переменных, параметров, констант, типов, меток и спрайтов
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

    # Левая часть объявления с присваиванием: int a = 1; или let a = 1;
    if (
        parent.type == "assignment_expression"
        and parent.parent
        and parent.parent.type == "declaration_statement"
    ):
        left = parent.child_by_field_name("left")
        if left and left.id == node.id:
            return True

    # Переменные цикла foreach (t, score : list)
    if parent.type == "foreach_statement":
        left = parent.child_by_field_name("left")
        val = parent.child_by_field_name("value")
        if (left and left.id == node.id) or (val and val.id == node.id):
            return True

    # Именованный аргумент функции: func(alpha: 0.5)
    if parent.type == "_function_expression_argument":
        name_node = parent.child_by_field_name("name")
        if name_node and name_node.id == node.id:
            return True

    return False


def check_undefined_identifiers(source: str) -> list[types.Diagnostic]:
    """Последовательно проверяет идентификаторы с учётом порядка объявления и блочных областей видимости."""
    tree = parser.parse(bytes(source, "utf-8"))
    extra_diagnostics: list[types.Diagnostic] = []

    def walk_tree(node: Node, current_class: str | None = None) -> None:
        if node.type in ("class_definition", "struct_definition"):
            name_node = node.child_by_field_name("name")
            cls_name = (
                source[name_node.start_byte : name_node.end_byte].lower()
                if name_node
                else current_class
            )
            for ch in node.children:
                walk_tree(ch, cls_name)
            return

        if node.type == "method_declaration":
            body = next((c for c in node.children if c.type == "block"), None)
            if not body:
                return

            # Базовый scope: встроенные имена + глобальные символы + члены класса + параметры метода
            base_scope = set(builtin_identifiers)
            base_scope.update(symbol_index.keys())
            if current_class:
                for m in get_members_with_inheritance(current_class):
                    base_scope.add(m.orig_name.lower())

            param_list = next(
                (c for c in node.children if c.type == "parameter_list"), None
            )
            if param_list:
                for n_node in param_list.children_by_field_name("name"):
                    base_scope.add(source[n_node.start_byte : n_node.end_byte].lower())

            scope_stack: list[set[str]] = [base_scope]

            def is_declared(name_lower: str) -> bool:
                return any(name_lower in scope for scope in reversed(scope_stack))

            def check_node(n: Node) -> None:
                # 1. Вложенный блок { ... } создаёт локальную область видимости
                if n.type == "block":
                    scope_stack.append(set())
                    for ch in n.children:
                        check_node(ch)
                    scope_stack.pop()
                    return

                # 2. Цикл for (int i = 0; ...) изолирует счётчик i внутри цикла
                if n.type == "for_statement":
                    scope_stack.append(set())
                    for ch in n.children:
                        check_node(ch)
                    scope_stack.pop()
                    return

                # 3. Цикл foreach (k, v : list) сначала проверяет list, затем регистрирует k и v
                if n.type == "foreach_statement":
                    right_node = n.child_by_field_name("right")
                    if right_node:
                        check_node(right_node)

                    scope_stack.append(set())
                    for field_name in ("left", "value"):
                        f_node = n.child_by_field_name(field_name)
                        if f_node and f_node.type == "identifier":
                            scope_stack[-1].add(
                                source[f_node.start_byte : f_node.end_byte].lower()
                            )

                    body_node = n.child_by_field_name("body")
                    if body_node:
                        check_node(body_node)
                    scope_stack.pop()
                    return

                # 4. Объявление переменной: сначала проверяем правую часть, потом добавляем имя в текущий scope
                if n.type == "declaration_statement":
                    for child in n.children:
                        if child.type == "identifier":
                            scope_stack[-1].add(
                                source[child.start_byte : child.end_byte].lower()
                            )
                        elif child.type == "assignment_expression":
                            right = child.child_by_field_name("right")
                            if right:
                                check_node(right)
                            left = child.child_by_field_name("left")
                            if left and left.type == "identifier":
                                scope_stack[-1].add(
                                    source[left.start_byte : left.end_byte].lower()
                                )
                    return

                # 5. Локальная константа внутри метода
                if n.type in ("const_definition", "const_array_definition"):
                    val_node = n.child_by_field_name("value") or n.child_by_field_name(
                        "contents"
                    )
                    if val_node:
                        check_node(val_node)
                    name_node = n.child_by_field_name("name")
                    if name_node:
                        scope_stack[-1].add(
                            source[name_node.start_byte : name_node.end_byte].lower()
                        )
                    return

                # 6. Обычный идентификатор в выражении
                if n.type == "identifier" and not is_ignored_identifier(n):
                    orig_text = source[n.start_byte : n.end_byte]
                    if not is_declared(orig_text.lower()):
                        extra_diagnostics.append(
                            types.Diagnostic(
                                range=node_to_range(n),
                                message=f"Unknown identifier '{orig_text}'",
                                severity=types.DiagnosticSeverity.Error,
                            )
                        )
                    return

                for ch in n.children:
                    check_node(ch)

            check_node(body)
            return

        for ch in node.children:
            walk_tree(ch, current_class)

    walk_tree(tree.root_node)
    return extra_diagnostics

# endregion


@server.feature(types.INITIALIZED)
def on_initialized(ls: LanguageServer, params: types.InitializedParams) -> None:
    try:
        global keywords
        keywords_path = Path(__file__).parent / "keywords.txt"
        keywords = [
            line
            for line in keywords_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

        global builtin_identifiers
        identifiers_path = Path(__file__).parent / "identifiers.txt"
        builtin_identifiers = [
            line
            for line in identifiers_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
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
                        index_source(
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
        for kw in keywords:
            items.append(
                types.CompletionItem(label=kw, kind=types.CompletionItemKind.Keyword)
            )
        allowed_global_kinds = {
            types.CompletionItemKind.Class,
            types.CompletionItemKind.Struct,
            types.CompletionItemKind.Enum,
        }
        for entries in symbol_index.values():
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
    tree = parser.parse(bytes(doc.source, "utf8"))
    point = (params.position.line, params.position.character)
    node = tree.root_node.descendant_for_point_range(point, point)
    if not node:
        return None

    target_key = (
        doc.source[node.start_byte : node.end_byte].strip('"').strip("'").lower()
    )

    # Checking whether the word under the cursor is a class member following a dot (for example: MyLibrary.GetInstance)
    filtered_entries: list[SymbolInfo] | None = None
    parent = node.parent
    if parent and parent.type == "member_access_expression":
        member_node = parent.child_by_field_name("member")
        if member_node and member_node.id == node.id and parent.children:
            obj_node = parent.children[0]
            obj_ident = doc.source[obj_node.start_byte : obj_node.end_byte].lower()
            resolved_owner = resolve_identifier_type(obj_ident, doc.source, point)
            if resolved_owner:
                # Search for a method/field only in the inheritanche chain of this class
                members = get_members_with_inheritance(resolved_owner)
                matched = [m for m in members if m.orig_name.lower() == target_key]
                if matched:
                    filtered_entries = matched

    # If this is not a dot-notation call (or the type on the left could not be determined), retrieve it from the general index
    entries = (
        filtered_entries
        if filtered_entries is not None
        else symbol_index.get(target_key)
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


@server.feature(types.WORKSPACE_DID_CHANGE_CONFIGURATION)
def did_change_configuration(
    ls: LanguageServer, params: types.DidChangeConfigurationParams
) -> None:
    pass


if __name__ == "__main__":
    server.start_io()
