from pathlib import Path
from urllib.parse import unquote, urlparse

import tree_sitter_zscript as tszscript
from lsprotocol import types
from pygls.lsp.server import LanguageServer
from tree_sitter import Language, Node, Parser

ZSCRIPT_LANGUAGE = Language(tszscript.language())
parser = Parser(ZSCRIPT_LANGUAGE)

server = LanguageServer("zscript-lsp", "0.1.0")

symbol_index: dict[str, list[types.Location]] = {}
symbol_docs: dict[
    str, list[tuple[str, str]]
] = {}  # { "name": [(uri, "signature"), ...] }
file_symbols: dict[str, set[str]] = {}


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


def extract_signature(node: Node, source: str) -> str:
    end_byte = node.end_byte
    for child in node.children:
        if child.type in ("block", ";"):
            end_byte = child.start_byte
            break
    return " ".join(source[node.start_byte : end_byte].split())


def index_source(uri: str, source: str) -> list[types.Diagnostic]:
    """Парсит файл, собирает диагностику ошибок и обновляет индекс символов."""
    # Clearing old symbols from this file before reindexing
    old_names = file_symbols.get(uri, set())
    for name in old_names:
        if name in symbol_index:
            symbol_index[name] = [loc for loc in symbol_index[name] if loc.uri != uri]
            if not symbol_index[name]:
                del symbol_index[name]
    file_symbols[uri] = set()

    tree = parser.parse(bytes(source, "utf8"))
    diagnostics: list[types.Diagnostic] = []

    def add_symbol(name_node: Node | None, decl_node: Node) -> None:
        if not name_node:
            return
        name_text = source[name_node.start_byte : name_node.end_byte].lower()
        location = types.Location(uri=uri, range=node_to_range(name_node))
        signature = extract_signature(decl_node, source)

        symbol_index.setdefault(name_text, []).append(location)
        symbol_docs.setdefault(name_text, []).append((uri, signature))
        file_symbols[uri].add(name_text)

    def walk(node: Node) -> None:
        if node.type == "ERROR" or node.is_missing:
            diagnostics.append(
                types.Diagnostic(
                    range=node_to_range(node),
                    message="ZScript Syntax Error",
                    severity=types.DiagnosticSeverity.Error,
                )
            )

        # Index declarations of classes, structures, methods, constants, enums, and states.
        if node.type in (
            "class_definition",
            "struct_definition",
            "enum_declaration",
            "method_declaration",
            "const_definition",
            "const_array_definition",
            "enum_declaration_value",
        ):
            add_symbol(node.child_by_field_name("name"), node)
        elif node.type == "state_label":
            add_symbol(node.child_by_field_name("label"), node)

        for child in node.children:
            walk(child)

    walk(tree.root_node)
    return diagnostics


def validate_and_index(ls: LanguageServer, uri: str, source: str) -> None:
    diagnostics = index_source(uri, source)
    ls.text_document_publish_diagnostics(
        types.PublishDiagnosticsParams(uri=uri, diagnostics=diagnostics)
    )


@server.feature(types.INITIALIZED)
def on_initialized(ls: LanguageServer, params: types.InitializedParams) -> None:
    """Upon server startup, scans all .zs / .zsc files in the working directory."""
    for folder in ls.workspace.folders.values():
        root_path = uri_to_path(folder.uri)
        if not root_path.exists():
            continue
        for ext in ("*.zs", "*.zsc", "*.zscript"):
            for file_path in root_path.rglob(ext):
                try:
                    text = file_path.read_text(encoding="utf-8", errors="ignore")
                    index_source(file_path.as_uri(), text)
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


@server.feature(types.TEXT_DOCUMENT_DEFINITION)
def goto_definition(
    ls: LanguageServer, params: types.DefinitionParams
) -> list[types.Location] | None:
    doc = ls.workspace.get_text_document(params.text_document.uri)
    tree = parser.parse(bytes(doc.source, "utf8"))

    point = (params.position.line, params.position.character)
    node = tree.root_node.descendant_for_point_range(point, point)
    if not node:
        return None

    # Supports referencing by identifier (e.g., Weapon, A_Jump)
    # as well as by class name enclosed in quotation marks (e.g., "InsertedShell")
    target_text = (
        doc.source[node.start_byte : node.end_byte].strip('"').strip("'").lower()
    )
    if not target_text:
        return None

    return symbol_index.get(target_text)


@server.feature(types.TEXT_DOCUMENT_COMPLETION)
def completions(params: types.CompletionParams) -> types.CompletionList:
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
        "foreach",
    ]
    items = [
        types.CompletionItem(label=kw, kind=types.CompletionItemKind.Keyword)
        for kw in keywords
    ]
    # Add autocomplete for all indexed files
    for sym_name in symbol_index:
        items.append(
            types.CompletionItem(
                label=sym_name,
                kind=types.CompletionItemKind.Function,
                detail=symbol_index[sym_name][0].uri,
            )
        )
    return types.CompletionList(is_incomplete=False, items=items)


@server.feature(types.TEXT_DOCUMENT_HOVER)
def hover(ls: LanguageServer, params: types.HoverParams) -> types.Hover | None:
    doc = ls.workspace.get_text_document(params.text_document.uri)
    tree = parser.parse(bytes(doc.source, "utf8"))

    point = (params.position.line, params.position.character)
    node = tree.root_node.descendant_for_point_range(point, point)
    if not node:
        return None

    target = doc.source[node.start_byte : node.end_byte].strip("'").strip('"').lower()
    entries = symbol_docs.get(target)
    if not entries:
        return None

    # Remove duplicate signatures (for example, if a method has been overridden)
    unique_sigs = list(dict.fromkeys(sig for _, sig in entries))
    markdown = "\n\n---\n\n".join(f"```zscript\n{sig}\n```" for sig in unique_sigs)

    return types.Hover(
        contents=types.MarkupContent(kind=types.MarkupKind.Markdown, value=markdown),
        range=node_to_range(node),
    )


@server.feature(types.WORKSPACE_DID_CHANGE_CONFIGURATION)
def did_change_configuration(
    ls: LanguageServer, params: types.DidChangeConfigurationParams
) -> None:
    pass


if __name__ == "__main__":
    server.start_io()
