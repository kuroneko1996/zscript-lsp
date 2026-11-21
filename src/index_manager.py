import zipfile
from pathlib import Path

from lsprotocol import types
from tree_sitter import Node, Parser

from symbol_info import SymbolInfo
from utils import (
    extract_signature,
    find_owner_node,
    node_text,
    node_to_range,
)
from zscript_visitor import ZScriptVisitor


class IndexManager:
    def __init__(self, parser: Parser) -> None:
        # Global index: { "lower_case_name": [SymbolInfo, ...] }
        self.symbol_index: dict[str, list[SymbolInfo]] = {}
        # Class index: { "lower_case_class_name": [SymbolInfo, ...] }
        self.class_members: dict[str, list[SymbolInfo]] = {}
        # Inheritance {"child_class_lower_case": "parent_class_lower_case"}
        self.class_parents: dict[str, str] = {}

        # Links symbols to files for quick clearing or reindexing
        self.file_symbols: dict[str, set[str]] = {}
        self.file_classes: dict[str, set[str]] = {}

        self.indexed_pk3s: set[str] = set()

        self.parser = parser

    def get_symbol(self, key: str) -> list[SymbolInfo] | None:
        return self.symbol_index.get(key)

    def add_symbol(
        self,
        source: str,
        uri: str,
        name_node: Node,
        decl_node: Node,
        kind: types.CompletionItemKind,
    ) -> None:
        orig_name = node_text(name_node, source)
        key = orig_name.lower()
        owner_info = find_owner_node(decl_node)
        owner_name = owner_info[1] if owner_info else None
        type_node = decl_node.child_by_field_name("type")
        type_name = (
            node_text(type_node, source).strip("@").lower() if type_node else None
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

        self.symbol_index.setdefault(key, []).append(info)
        self.file_symbols[uri].add(key)

        if owner_name:
            owner_key = owner_name.lower()
            self.class_members.setdefault(owner_key, []).append(info)
            self.file_classes[uri].add(owner_key)

    def clear_file_index(self, uri: str) -> None:
        for name in self.file_symbols.get(uri, set()):
            if name in self.symbol_index:
                self.symbol_index[name] = [
                    s for s in self.symbol_index[name] if s.uri != uri
                ]
                if not self.symbol_index[name]:
                    del self.symbol_index[name]
        self.file_symbols[uri] = set()

        for cls_name in self.file_classes.get(uri, set()):
            if cls_name in self.class_members:
                self.class_members[cls_name] = [
                    m for m in self.class_members[cls_name] if m.uri != uri
                ]
                if not self.class_members[cls_name]:
                    del self.class_members[cls_name]
        self.file_classes[uri] = set()

    def handle_include(self, target_uri: str, target_file: Path) -> None:
        """Handles dependency resolution and recursive indexing."""
        if target_uri not in self.file_symbols:
            try:
                inc_source = target_file.read_text(encoding="utf-8", errors="ignore")
                self.index_source(target_uri, inc_source, None, None)
            except OSError:
                pass

    def is_known_class_or_struct(self, type_lower: str) -> bool:
        if type_lower in self.class_members:
            return True
        for entry in self.get_symbol(type_lower) or []:
            if entry.kind in (
                types.CompletionItemKind.Class,
                types.CompletionItemKind.Struct,
            ):
                return True
        return False

    def index_source(
        self,
        uri: str,
        source: str,
        workspace_root: Path | None = None,
        visited_uris: set[str] | None = None,
    ) -> list[types.Diagnostic]:
        if visited_uris is None:
            visited_uris = {uri}
        else:
            visited_uris.add(uri)

        self.clear_file_index(uri)

        # The Parser object needs access to source content for node_text helpers.
        # We'll pass this context to the visitor.

        tree = self.parser.parse(bytes(source, "utf-8"))
        diagnostics: list[types.Diagnostic] = []

        visitor = ZScriptVisitor(self)
        visitor.visit_root_node(
            tree.root_node, source, uri, workspace_root, diagnostics
        )

        return diagnostics

    def index_pk3(self, pk3_path_str: str) -> None:
        """Читает все ZScript-файлы напрямую из ZIP/PK3 архива движка."""
        pk3_path = Path(pk3_path_str).expanduser().resolve()
        if not pk3_path.is_file() or str(pk3_path) in self.indexed_pk3s:
            return

        self.indexed_pk3s.add(str(pk3_path))
        with zipfile.ZipFile(pk3_path, "r") as zf:
            # Ищем корневой zscript.zs / zscript.txt внутри архива или все .zs/.zsc
            all_names = zf.namelist()
            zscript_entries = [
                name
                for name in all_names
                if name.lower().startswith("zscript")
                and name.lower().endswith((".zs", ".zsc", ".zscript", ".zc"))
            ]

            for entry_name in zscript_entries:
                raw_bytes = zf.read(entry_name)
                text = raw_bytes.decode("utf-8", errors="ignore")
                # Формируем виртуальный URI вида zip:///C:/.../uzdoom.pk3/zscript/actors/actor.zs
                virtual_uri = f"zip:///{pk3_path.as_posix().lstrip('/')}/{entry_name}"
                self.index_source(uri=virtual_uri, source=text)

    def get_members_with_inheritance(self, class_key: str) -> list[SymbolInfo]:
        result: list[SymbolInfo] = []
        seen_members: set[str] = set()
        visited_classes: set[str] = set()

        curr = class_key.lower()
        while curr and curr not in visited_classes:
            visited_classes.add(curr)
            for member in self.class_members.get(curr, []):
                m_key = member.orig_name.lower()
                if m_key not in seen_members:
                    seen_members.add(m_key)
                    result.append(member)
            next_parent = self.class_parents.get(curr, "")
            if not next_parent and curr != "object" and "object" not in visited_classes:
                next_parent = "object"
            curr = next_parent

        return result
