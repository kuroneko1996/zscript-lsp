import re
from pathlib import Path

uzdoom_src = Path(r"F:\projects\uzdoom\src")  # путь к папке src исходников UZDoom
lsp_src = Path(r"F:\projects\zscript-lsp\src")

flags_file = lsp_src / "builtin_actor_flags.txt"
identifiers_file = lsp_src / "identifiers.txt"

# --- ЧАСТЬ 1: Сбор всех флагов Actor (в builtin_actor_flags.txt) ---

re_define_flag = re.compile(
    r"\bDEFINE_(?:ACTOR_)?FLAG\d*(?:_BIT)?\s*\(\s*[^,]+,\s*([a-zA-Z_]\w*)",
    re.MULTILINE,
)
re_dep_flag = re.compile(
    r"\bDEFINE_DEPRECATED_FLAG\s*\(\s*([a-zA-Z_]\w*)",
    re.MULTILINE,
)
# Ищем блоки enum ActorFlag / ActorFlag2..9 / ActorRenderFlag / ActorRenderFlag2
re_actor_flag_enum = re.compile(
    r"\benum\s+Actor(?:Render)?Flag\d*\s*\{([^}]*)\}",
    re.DOTALL,
)
re_enum_item = re.compile(r"\b(?:MF\d*|RF\d*)_([A-Z0-9_]+)\b")

actor_flags: set[str] = set()

for path in list(uzdoom_src.rglob("*.cpp")) + list(uzdoom_src.rglob("*.h")):
    text = path.read_text(encoding="utf-8", errors="ignore")

    for name in re_define_flag.findall(text) + re_dep_flag.findall(text):
        low = name.lower()
        actor_flags.add(low if low.startswith("b") else f"b{low}")

    for enum_body in re_actor_flag_enum.findall(text):
        for raw_flag in re_enum_item.findall(enum_body):
            actor_flags.add(f"b{raw_flag.lower()}")

flags_file.write_text("\n".join(sorted(actor_flags)) + "\n", encoding="utf-8")


# --- ЧАСТЬ 2: Сбор глобальных CVar, функций и DEFINE_SPECIAL (в identifiers.txt) ---

existing_idents = {
    line.strip().lower()
    for line in identifiers_file.read_text(encoding="utf-8").splitlines()
    if line.strip()
}

re_cvar = re.compile(
    r"\b(?:CUSTOM_)?CVAR(?:_NAMED)?\s*\(\s*\w+\s*,\s*([a-zA-Z_]\w*)",
    re.MULTILINE,
)
re_global_fn = re.compile(
    r"\bDEFINE_GLOBAL_FUNCTION\s*\(\s*([a-zA-Z_]\w*)",
    re.MULTILINE,
)
re_action_fn = re.compile(
    r"\bDEFINE_ACTION_FUNCTION(?:_NATIVE)?\s*\(\s*(?:_ZScript|_Native)\s*,\s*([a-zA-Z_]\w*)",
    re.IGNORECASE | re.MULTILINE,
)
# DEFINE_SPECIAL(Polyobj_StopSound, 283, 1, 1, 1)
re_special = re.compile(
    r"\bDEFINE_SPECIAL\s*\(\s*([a-zA-Z_]\w*)",
    re.MULTILINE,
)

global_idents: set[str] = set(existing_idents)

for path in list(uzdoom_src.rglob("*.cpp")) + list(uzdoom_src.rglob("*.h")):
    text = path.read_text(encoding="utf-8", errors="ignore")
    global_idents.update(m.lower() for m in re_cvar.findall(text))
    global_idents.update(m.lower() for m in re_global_fn.findall(text))
    global_idents.update(m.lower() for m in re_action_fn.findall(text))
    global_idents.update(m.lower() for m in re_special.findall(text))

identifiers_file.write_text("\n".join(sorted(global_idents)) + "\n", encoding="utf-8")

print(f"Флагов Actor: {len(actor_flags)} (bnosector: {'bnosector' in actor_flags}, bdropped: {'bdropped' in actor_flags})")
print(f"Глобальных идентификаторов: {len(global_idents)} (polyobj_stopsound: {'polyobj_stopsound' in global_idents})")
