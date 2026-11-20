import re
from pathlib import Path

uzdoom_src = Path(r"F:\\projects\\uzdoom\\src")  # Укажите путь к папке src исходников UZDoom
identifiers_file = Path(r"F:\projects\zscript-lsp\src\identifiers.txt")

existing = {
    line.strip().lower()
    for line in identifiers_file.read_text(encoding="utf-8").splitlines()
    if line.strip()
}

# 1. Все CVar: CVAR(Type, name, ...), CUSTOM_CVAR(...), CVAR_NAMED(...)
re_cvar = re.compile(
    r"\b(?:CUSTOM_)?CVAR(?:_NAMED)?\s*\(\s*\w+\s*,\s*([a-zA-Z_]\w*)",
    re.MULTILINE,
)

# 2. Глобальные нативные функции: DEFINE_GLOBAL_FUNCTION(FuncName)
re_global_fn = re.compile(
    r"\bDEFINE_GLOBAL_FUNCTION\s*\(\s*([a-zA-Z_]\w*)",
    re.MULTILINE,
)

# 3. Экшен-функции, объявленные в глобальном пространстве _ZScript (или _Native):
# DEFINE_ACTION_FUNCTION(_ZScript, FuncName) / DEFINE_ACTION_FUNCTION_NATIVE(_ZScript, FuncName, ...)
re_action_fn = re.compile(
    r"\bDEFINE_ACTION_FUNCTION(?:_NATIVE)?\s*\(\s*(?:_ZScript|_Native)\s*,\s*([a-zA-Z_]\w*)",
    re.IGNORECASE | re.MULTILINE,
)

found: set[str] = set()

for cpp_file in uzdoom_src.rglob("*.cpp"):
    text = cpp_file.read_text(encoding="utf-8", errors="ignore")
    found.update(m.lower() for m in re_cvar.findall(text))
    found.update(m.lower() for m in re_global_fn.findall(text))
    found.update(m.lower() for m in re_action_fn.findall(text))

# Также проверяем заголовочные файлы .h на случай объявлений макросов там
for h_file in uzdoom_src.rglob("*.h"):
    text = h_file.read_text(encoding="utf-8", errors="ignore")
    found.update(m.lower() for m in re_cvar.findall(text))
    found.update(m.lower() for m in re_global_fn.findall(text))
    found.update(m.lower() for m in re_action_fn.findall(text))


merged = sorted(existing | found)
identifiers_file.write_text("\n".join(merged) + "\n", encoding="utf-8")

print(f"Было: {len(existing)}")
print(f"Найдено в UZDoom: {len(found)}")
print(f"Добавлено новых: {len(merged) - len(existing)}")
print(f"Итого в identifiers.txt: {len(merged)}")


# BUILTIN ACTOR FLAGS
flags_file = Path(r"F:\projects\zscript-lsp\src\builtin_actor_flags.txt")

# 1. DEFINE_FLAG / DEFINE_FLAG2..9 / DEFINE_ACTOR_FLAG / DEFINE_FLAG_BIT(MASK, FlagName, ...)
re_flag = re.compile(
    r"\bDEFINE_(?:ACTOR_)?FLAG\d*(?:_BIT)?\s*\(\s*[^,]+,\s*([a-zA-Z_]\w*)",
    re.MULTILINE,
)

# 2. DEFINE_DEPRECATED_FLAG(FlagName, ...)
re_dep_flag = re.compile(
    r"\bDEFINE_DEPRECATED_FLAG\s*\(\s*([a-zA-Z_]\w*)",
    re.MULTILINE,
)

flags: set[str] = set()
for cpp_file in uzdoom_src.rglob("*.cpp"):
    text = cpp_file.read_text(encoding="utf-8", errors="ignore")
    for name in re_flag.findall(text) + re_dep_flag.findall(text):
        low = name.lower()
        flags.add(low if low.startswith("b") else f"b{low}")

flags_file.write_text("\n".join(sorted(flags)) + "\n", encoding="utf-8")
print(f"Собрано флагов Actor: {len(flags)}")
