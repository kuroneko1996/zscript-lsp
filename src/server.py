import zipfile

import tree_sitter_zscript as tszscript
from lsprotocol import types
from pygls.lsp.server import LanguageServer
from tree_sitter import Language, Node, Parser

from builtins_loader import load_all_builtins
from index_manager import IndexManager
from semantic_tokens import TOKEN_MODIFIERS, TOKEN_TYPES, build_semantic_tokens
from semantic_validator import SemanticValidator
from type_resolver import TypeResolver
from utils import (
    get_workspace_root_for_uri,
    node_text,
    node_to_range,
    resolve_include_path,
    uri_to_path,
)

ZSCRIPT_LANGUAGE = Language(tszscript.language())
parser = Parser(ZSCRIPT_LANGUAGE)
server = LanguageServer("zscript-lsp", "0.1.1")

index_manager = IndexManager(parser=parser)
type_resolver = TypeResolver(parser=parser, index_manager=index_manager)

keywords: list[str] = []
builtin_identifiers: list[str] = []


def apply_settings(raw_settings: object, ls: LanguageServer) -> None:
    if not isinstance(raw_settings, dict):
        return
    cfg = raw_settings.get("zscript-lsp", raw_settings)
    pk3_list = cfg.get("pk3_paths", []) if isinstance(cfg, dict) else []
    if not pk3_list:
        ls.window_show_message(
            types.ShowMessageParams(types.MessageType.Warning, "PK3 List is empty")
        )
        return

    loaded_count = 0
    for pk3 in pk3_list:
        if isinstance(pk3, str):
            try:
                index_manager.index_pk3(pk3)
                loaded_count += 1
            except (zipfile.BadZipfile, OSError) as err:
                ls.window_show_message(
                    types.ShowMessageParams(types.MessageType.Error, str(err))
                )

    ls.window_show_message(
        types.ShowMessageParams(
            types.MessageType.Info, f"{loaded_count} pk3(s) loaded."
        )
    )


def validate_and_index(ls: LanguageServer, uri: str, source: str) -> None:
    root = get_workspace_root_for_uri(ls, uri)
    diagnostics = index_manager.index_source(uri, source, workspace_root=root)
    validator = SemanticValidator(index_manager, builtin_identifiers)
    diagnostics.extend(validator.validate(source))
    ls.text_document_publish_diagnostics(
        types.PublishDiagnosticsParams(uri=uri, diagnostics=diagnostics)
    )


@server.feature(types.INITIALIZE)
def on_initialize(ls: LanguageServer, params: types.InitializeParams) -> None:
    if params.initialization_options:
        apply_settings(params.initialization_options, ls)


@server.feature(types.INITIALIZED)
def on_initialized(ls: LanguageServer, params: types.InitializedParams) -> None:
    global keywords, builtin_identifiers
    try:
        keywords, builtin_identifiers = load_all_builtins(index_manager)
    except OSError as err:
        ls.window_show_message(
            types.ShowMessageParams(types.MessageType.Error, str(err))
        )

    visited_uris: set[str] = set()
    for folder in ls.workspace.folders.values():
        root_path = uri_to_path(folder.uri).resolve()
        if not root_path.exists():
            continue

        for root_entry in (
            "zscript",
            "zscript.zs",
            "zscript.zsc",
            "zscript.zscript",
            "zscript.txt",
        ):
            entry_file = root_path / root_entry
            if entry_file.is_file() and entry_file.as_uri() not in visited_uris:
                try:
                    text = entry_file.read_text(encoding="utf-8", errors="ignore")
                    index_manager.index_source(
                        entry_file.as_uri(), text, root_path, visited_uris
                    )
                except OSError:
                    pass

        for ext in ("*.zs", "*.zsc", "*.zscript"):
            for file_path in root_path.rglob(ext):
                if file_path.as_uri() not in visited_uris:
                    try:
                        text = file_path.read_text(encoding="utf-8", errors="ignore")
                        index_manager.index_source(
                            file_path.as_uri(), text, root_path, visited_uris
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
    items: list[types.CompletionItem] = []
    doc = ls.workspace.get_text_document(params.text_document.uri)
    current_line = doc.lines[params.position.line][: params.position.character]
    point = (params.position.line, params.position.character)

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
            resolved_type = type_resolver.resolve_identifier_type(
                owner_ident, doc.source, point
            )
            target_class = resolved_type or owner_ident
            for m in index_manager.get_members_with_inheritance(target_class):
                sig_lines = m.signature.splitlines()
                signature = sig_lines[1].strip() if len(sig_lines) > 1 else m.signature
                items.append(
                    types.CompletionItem(
                        label=m.orig_name, kind=m.kind, detail=signature
                    )
                )
    else:
        items.extend(type_resolver.collect_visible_locals(doc.source, point, keywords))
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

    entries = type_resolver.resolve_symbol_at_point(doc.source, point, node)
    if not entries:
        return None

    unique_sigs = list(dict.fromkeys(e.signature for e in entries))
    markdown = "\n\n---\n\n".join(f"```zscript\n{sig}\n```" for sig in unique_sigs)
    return types.Hover(
        contents=types.MarkupContent(kind=types.MarkupKind.Markdown, value=markdown),
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

    # 1. Переход по #include
    curr: Node | None = node
    while curr:
        if curr.type == "include_definition":
            if path_node := curr.child_by_field_name("path"):
                root = get_workspace_root_for_uri(ls, doc.uri)
                target_file = resolve_include_path(
                    root, doc.uri, node_text(path_node, doc.source)
                )
                if target_file:
                    zero = types.Position(line=0, character=0)
                    return types.Location(
                        uri=target_file.as_uri(),
                        range=types.Range(start=zero, end=zero),
                    )
            return None
        curr = curr.parent

    # 2. Переход к символу (метод после точки, метод текущего класса или глобальный символ)
    entries = type_resolver.resolve_symbol_at_point(doc.source, point, node)
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
    return types.SemanticTokens(
        data=build_semantic_tokens(parser, index_manager, doc.source)
    )


@server.feature(types.WORKSPACE_DID_CHANGE_CONFIGURATION)
def did_change_configuration(
    ls: LanguageServer, params: types.DidChangeConfigurationParams
) -> None:
    if params.settings:
        apply_settings(params.settings, ls)


if __name__ == "__main__":
    server.start_io()
