from dataclasses import dataclass

from lsprotocol import types


@dataclass
class SymbolInfo:
    orig_name: str
    kind: types.CompletionItemKind
    uri: str
    location: types.Location
    signature: str
    owner: str | None = None
    type_name: str | None = None
