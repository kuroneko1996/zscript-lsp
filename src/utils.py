from pathlib import Path
from urllib.parse import unquote, urlparse

from lsprotocol import types
from pygls.lsp.server import LanguageServer
from tree_sitter import Node


def node_text(node: Node, source: str) -> str:
    source_bytes = bytes(source, "utf-8")
    return source_bytes[node.start_byte : node.end_byte].decode(
        "utf-8", errors="ignore"
    )


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

    # 1. Проверяем workspace.folders (Zed, Sublime, VS Code)
    for folder in ls.workspace.folders.values():
        folder_path = uri_to_path(folder.uri).resolve()
        if folder_path == file_path or folder_path in file_path.parents:
            return folder_path

    # 2. Проверяем root_uri / root_path (Neovim)
    if ls.workspace.root_uri:
        root_path = uri_to_path(ls.workspace.root_uri).resolve()
        if root_path == file_path or root_path in file_path.parents:
            return root_path

    # 3. Ищем по маркерам корня вверх от файла
    for parent in file_path.parents:
        for marker in (
            "zscript.zs",
            "zscript.zsc",
            "zscript.zscript",
            "zscript.txt",
            ".git",
        ):
            if (parent / marker).exists():
                return parent

    return None


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
        if parent.type in ("class_definition", "struct_definition", "extend_class"):
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

    source_bytes = bytes(source, "utf-8")
    sig = " ".join(
        source_bytes[node.start_byte : end_byte]
        .decode("utf-8", errors="ignore")
        .split()
    )

    if node.type in ("method_declaration", "variable_declaration"):
        sig += ";"
        owner = find_owner_node(node)
        if owner:
            kind, owner_name = owner
            return f"{kind} {owner_name} {{\n    {sig}\n}}"
    return sig
