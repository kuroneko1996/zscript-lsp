import tree_sitter_zscript as tszscript
from lsprotocol import types
from pygls.lsp.server import LanguageServer
from tree_sitter import Language, Node, Parser

ZSCRIPT_LANGUAGE = Language(tszscript.language())
parser = Parser(ZSCRIPT_LANGUAGE)

server = LanguageServer("zscript-lsp", "0.1.0")


def validate_document(ls: LanguageServer, uri: str, source: str) -> None:
    tree = parser.parse(bytes(source, "utf8"))
    diagnostics = []

    def walk(node: Node) -> None:
        if node.type == "ERROR" or node.is_missing:
            diagnostics.append(
                types.Diagnostic(
                    range=types.Range(
                        start=types.Position(
                            line=node.start_point[0],
                            character=node.start_point[1],
                        ),
                        end=types.Position(
                            line=node.end_point[0],
                            character=node.end_point[1],
                        ),
                    ),
                    message="ZScript Syntax error",
                    severity=types.DiagnosticSeverity.Error,
                )
            )
        for child in node.children:
            walk(child)

    walk(tree.root_node)
    ls.text_document_publish_diagnostics(
        types.PublishDiagnosticsParams(uri=uri, diagnostics=diagnostics)
    )


@server.feature(types.TEXT_DOCUMENT_DID_OPEN)
def did_open(ls: LanguageServer, params: types.DidOpenTextDocumentParams) -> None:
    doc = ls.workspace.get_text_document(params.text_document.uri)
    validate_document(ls, doc.uri, doc.source)


@server.feature(types.TEXT_DOCUMENT_DID_CHANGE)
def did_change(ls: LanguageServer, params: types.DidChangeTextDocumentParams) -> None:
    doc = ls.workspace.get_text_document(params.text_document.uri)
    validate_document(ls, doc.uri, doc.source)


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
    ]
    return types.CompletionList(
        is_incomplete=False,
        items=[
            types.CompletionItem(label=kw, kind=types.CompletionItemKind.Keyword)
            for kw in keywords
        ],
    )


@server.feature(types.WORKSPACE_DID_CHANGE_CONFIGURATION)
def did_change_configuration(
    ls: LanguageServer, params: types.DidChangeConfigurationParams
) -> None:
    pass


if __name__ == "__main__":
    server.start_io()
