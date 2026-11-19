# zscript-lsp

A lightweight Language Server Protocol (LSP) implementation for **ZScript** (GZDoom / UZDoom), powered by [pygls](https://github.com/openlawlibrary/pygls) and [tree-sitter-zscript](https://github.com/kuroneko1996/tree-sitter-zscript).

## Features

- **Syntax & Semantic Diagnostics:** Real-time syntax error reporting, block-scoped undeclared variable checks, declaration order validation, and unknown member access detection.
- **Context-Aware Autocompletion:**
  - Keywords, built-in types, classes, structs, enums, local variables, and method parameters.
  - Member completion after `.` with full class inheritance resolution (`Actor` → `Thinker` → `Object`).
  - Type inference for `let` bindings, `new("ClassName")` expressions, method return types, `self`, and `super`.
- **Rich Hover Information:** Syntax-highlighted function signatures, class headers, constants, and struct definitions in Markdown.
- **Go to Definition:** Jump to `#include` files, local variables, parameters, global symbols, and inherited class members.
- **Engine Archive Indexing:** Direct indexing of ZScript definitions from `uzdoom.pk3` or `gzdoom.pk3` without extracting files.

## Requirements

- Python 3.11+
- [uv](https://astral.sh) (recommended) or `pip`

## Installation

Clone the repository and install dependencies:

```bash
git clone https://github.com/kuroneko1996/zscript-lsp.git
cd zscript-lsp
uv sync
```

## Configuration (Zed)

To index built-in engine classes (such as `Actor`, `Weapon`, or `PlayerPawn`), specify the path to your `uzdoom.pk3` or `gzdoom.pk3` archive in your editor settings (`settings.json`):

```json
{
  "lsp": {
    "zscript-lsp": {
      "initialization_options": {
        "pk3_paths": ["C:/Games/UZDoom/uzdoom.pk3"]
      }
    }
  }
}
```

## Configuration (Sublime Text)

LSP Package is required.

User\LSP.sublime-settings

```json
{
  "clients": {
    "zscript-lsp": {
      "enabled": true,
      "command": [
        "uv",
        "run",
        "--project",
        "F:\\projects\\zscript-lsp",
        "F:\\projects\\zscript-lsp\\src\\server.py"
      ],
      "selector": "source.zscript",
      "initializationOptions": {
        "pk3_paths": ["C:\\Games\\UZDoom\\uzdoom.pk3"]
      }
    }
  }
}
```
[syntax highlighting](https://gist.github.com/kuroneko1996/11dc8284a8a12f3c5b01f1347a03ea7a)

place in Packages -> User

## Configuration (Neovim 0.11+)

```lua
-- Register extensions .zs / .zsc as zscript filetype
vim.filetype.add({
  extension = {
    zs = "zscript",
    zsc = "zscript",
    zscript = "zscript",
  },
})

-- Setup and enable zscript-lsp
vim.lsp.config("zscript_lsp", {
  cmd = {
    "uv",
    "run",
    "--project",
    "F:/projects/zscript-lsp",
    "F:/projects/zscript-lsp/src/server.py",
  },
  filetypes = { "zscript" },
  root_markers = { "zscript.zs", "zscript.zsc", "zscript.txt", ".git" },
  init_options = {
    pk3_paths = {
      "C:/Games/UZDoom/uzdoom.pk3",
    },
  },
})

vim.lsp.enable("zscript_lsp")
```

## Running Manually

The server communicates over `stdio` by default:

```bash
uv run src/server.py
```

## License

MIT
