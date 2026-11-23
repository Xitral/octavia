"""Tool registry and plugin loading.

Built-in tools live in ``octavia.tools.builtin``. Every module in
``octavia.tools.extensions`` is loaded too, so a new tool (for example one the
self-improvement builder wrote) needs no registration anywhere. Extra tool
modules can be listed in config (``tools.plugins``). Each module must expose
either a ``register(registry)`` function or a ``TOOLS`` list of Tool
classes/instances.
"""

from __future__ import annotations

import fnmatch
import importlib
import logging
import pkgutil
from collections.abc import Iterable

from octavia.tools.base import Tool, ToolSpec

log = logging.getLogger(__name__)

BUILTIN_MODULES = (
    "octavia.tools.builtin.time_tools",
    "octavia.tools.builtin.system_tools",
    "octavia.tools.builtin.app_tools",
    "octavia.tools.builtin.web_tools",
    "octavia.tools.builtin.memory_tools",
)
EXTENSIONS_PACKAGE = "octavia.tools.extensions"


def extension_modules() -> list[str]:
    """Modules in ``octavia.tools.extensions`` (sorted, so the load order is stable)."""
    try:
        package = importlib.import_module(EXTENSIONS_PACKAGE)
    except ImportError:
        return []
    names = [m.name for m in pkgutil.iter_modules(package.__path__) if not m.name.startswith("_")]
    return [f"{EXTENSIONS_PACKAGE}.{name}" for name in sorted(names)]


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool | type[Tool]) -> None:
        instance = tool() if isinstance(tool, type) else tool
        name = instance.name
        if not name or not name.replace("_", "").isalnum():
            raise ValueError(f"Invalid tool name {name!r}: use letters, digits and underscores")
        if name in self._tools:
            raise ValueError(f"Tool {name!r} is already registered")
        self._tools[name] = instance

    def unregister(self, name: str) -> None:
        self._tools.pop(name, None)

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return sorted(self._tools)

    def all(self) -> list[Tool]:
        return [self._tools[n] for n in self.names()]

    def specs(self) -> list[ToolSpec]:
        return [t.spec() for t in self.all()]

    def __contains__(self, name: object) -> bool:
        return name in self._tools

    def __len__(self) -> int:
        return len(self._tools)

    def load_module(self, module_name: str) -> int:
        """Import a tool module and register what it exports. Returns the count added."""
        module = importlib.import_module(module_name)
        before = len(self._tools)
        if hasattr(module, "register"):
            module.register(self)
        elif hasattr(module, "TOOLS"):
            for tool in module.TOOLS:
                self.register(tool)
        else:
            raise ValueError(f"Tool module {module_name!r} has neither register() nor TOOLS")
        return len(self._tools) - before

    def apply_filters(self, enabled: Iterable[str], disabled: Iterable[str]) -> None:
        """Keep tools matching any ``enabled`` glob and no ``disabled`` glob."""
        enabled, disabled = list(enabled), list(disabled)
        for name in list(self._tools):
            keep = any(fnmatch.fnmatchcase(name, p) for p in enabled) and not any(
                fnmatch.fnmatchcase(name, p) for p in disabled
            )
            if not keep:
                del self._tools[name]


def build_registry(
    plugins: Iterable[str] = (),
    enabled: Iterable[str] = ("*",),
    disabled: Iterable[str] = (),
    include_builtins: bool = True,
    core: Iterable[Tool | type[Tool]] = (),
) -> ToolRegistry:
    """``core`` tools are registered first, so a later module can't take their names (it fails to load)."""
    registry = ToolRegistry()
    for tool in core:
        registry.register(tool)
    modules = [
        *(BUILTIN_MODULES if include_builtins else ()),
        *(extension_modules() if include_builtins else ()),
        *plugins,
    ]
    for module_name in modules:
        try:
            registry.load_module(module_name)
        except Exception:
            # A broken plugin must not take the assistant down.
            log.exception("Failed to load tool module %s", module_name)
    registry.apply_filters(enabled, disabled)
    return registry
