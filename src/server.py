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


@dataclass
class SymbolInfo:
    orig_name: str
    kind: types.CompletionItemKind
    uri: str
    location: types.Location
    signature: str
    owner: str | None = None


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
    if len(path_str) >= 3 and path_str == "/" and path_str == ":":
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
    root_path: Path, current_uri: str, inc_str: str
) -> Path | None:
    clean_rel = inc_str.strip('"').strip("'").replace("\\", "/").strip()
    if not clean_rel:
        return None
    candidate_root = (root_path / clean_rel).resolve()
    if candidate_root.is_file():
        return candidate_root
    current_dir = uri_to_path(current_uri).parent
    candidate_rel = (current_dir / clean_rel).resolve()
    if candidate_rel.is_file():
        return candidate_rel
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

        info = SymbolInfo(
            orig_name=orig_name,
            kind=kind,
            uri=uri,
            location=types.Location(uri=uri, range=node_to_range(name_node)),
            signature=extract_signature(decl_node, source),
            owner=owner_name,
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
            if path_node and workspace_root:
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
    diagnostics = index_source(uri, source)
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


@server.feature(types.INITIALIZED)
def on_initialized(ls: LanguageServer, params: types.InitializedParams) -> None:
    visited_uris: set[str] = set()
    for folder in ls.workspace.folders.values():
        root_path = uri_to_path(folder.uri).resolve()
        if not root_path.exists():
            continue

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
                    for ext in (".zs", ".zsc", "*.zscript"):
                        for file_path in root_path.rglob(ext):
                            file_uri = file_path.as_uri()
                            if file_uri not in visited_uris:
                                try:
                                    text = file_path.read_text(
                                        encoding="utf-8", errors="ignore"
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
    # Default items
    keywords = [
        "class",
        "struct",
        "enum",
        "States",
        "Default",
        "override",
        "clearscope",
        "play",
        "ui",
        "action",
        "static",
        "const",
        "let",
        "return",
        "for",
        "foreach",
    ]
    items = []
    for symbol_info in symbol_index.values():
        unique_names = list(dict.fromkeys(e.orig_name for e in symbol_info))
        items.append(types.CompletionItem(label=unique_names[0], kind=types.CompletionItemKind.Keyword))

    doc = ls.workspace.get_text_document(params.text_document.uri)
    current_line = doc.lines[params.position.line].strip()

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
            members = get_members_with_inheritance(owner_ident)
            for m in members:
                signature = m.signature.splitlines()[1].strip()
                items.append(
                    types.CompletionItem(
                        label=m.orig_name,
                        kind=m.kind,
                        detail=signature,
                    )
                )
    else:
        items = [
            types.CompletionItem(label=kw, kind=types.CompletionItemKind.Keyword)
            for kw in keywords
        ]

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
    entries = symbol_index.get(target_key)
    if not entries:
        return None
    unique_sigs = list(dict.fromkeys(e.signature for e in entries))
    markdown = "\n\n---\n\n".join(f"zscript\n{sig}\n" for sig in unique_sigs)
    markdown = "```" + markdown + "```"
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
