from pathlib import Path

from lsprotocol import types

from index_manager import IndexManager
from symbol_info import SymbolInfo


def load_word_list(filename: str) -> list[str]:
    path = Path(__file__).parent / filename
    if not path.is_file():
        return []
    return [
        line.strip().lower()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def load_object_methods(index_manager: IndexManager) -> None:
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
    dummy_loc = types.Location(
        uri="builtin://object",
        range=types.Range(
            start=types.Position(line=0, character=0),
            end=types.Position(line=0, character=0),
        ),
    )
    for m_name, m_sig, m_type, m_kind in builtin_object_methods:
        if m_name.lower() not in existing_obj:
            index_manager.class_members.setdefault("object", []).append(
                SymbolInfo(
                    orig_name=m_name,
                    kind=m_kind,
                    uri="builtin://object",
                    location=dummy_loc,
                    signature=f"class Object {{\n    {m_sig}\n}}",
                    owner="Object",
                    type_name=m_type,
                )
            )


def register_builtin_zscript_members(index_manager: IndexManager) -> None:
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
    existing = {
        m.orig_name.lower() for m in index_manager.class_members.get("actor", [])
    }
    if "default" not in existing:
        index_manager.class_members.setdefault("actor", []).append(default_prop)
        index_manager.symbol_index.setdefault("default", []).append(default_prop)


def load_actors_builtin_actor_flags(index_manager: IndexManager) -> None:
    flags_path = Path(__file__).parent / "builtin_actor_flags.txt"
    if not flags_path.is_file():
        return
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
        orig = (
            f"b{flag_name[1:].capitalize()}" if flag_name.startswith("b") else flag_name
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


def load_all_builtins(index_manager: IndexManager) -> tuple[list[str], list[str]]:
    keywords = load_word_list("keywords.txt")
    builtin_identifiers = load_word_list("identifiers.txt")
    load_object_methods(index_manager)
    register_builtin_zscript_members(index_manager)
    load_actors_builtin_actor_flags(index_manager)
    return keywords, builtin_identifiers
