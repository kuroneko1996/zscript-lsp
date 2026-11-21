class SymbolEntry:
    """A simple container for a symbol's type within a scope."""

    __slots__ = ("name", "type")

    def __init__(self, name: str, type_str: str | None):
        self.name = name
        self.type = type_str


class Scope:
    """
    Manages a single lexical scope, holding symbol definitions.
    Scopes are designed to be nested and searchable hierarchically.
    """

    def __init__(self, parent: "Scope | None" = None):
        # Symbols are stored in lower_case for case-insensitive lookup
        self.symbols: dict[str, SymbolEntry] = {}
        self.parent = parent

    def define_symbol(self, name: str, type_str: str | None) -> None:
        """Defines a new symbol in this scope."""
        self.symbols[name.lower()] = SymbolEntry(name.lower(), type_str)

    def lookup_symbol(self, name: str) -> SymbolEntry | None:
        """
        Looks up a symbol, checking the current scope and recursively checking parents.
        """
        name = name.lower()
        if name in self.symbols:
            return self.symbols[name]

        if self.parent:
            return self.parent.lookup_symbol(name)

        return None
