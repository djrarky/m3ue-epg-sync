"""
sources - Pluggable channel data sources, organized by provider.

Each source module defines where to fetch channel data and how to parse it
into a standardized (number, name, category, aliases) format.

Provider-specific sources live in subdirectories (e.g. sources/sky/).
Shared sources live at the top level (e.g. sources/csv.py).

Source modules implement one of two interfaces:

  Web sources (fetched via scraper):
    def fetch(**kwargs) -> list[tuple[int, str, str, list[str]]]:
        '''Fetch content, resolve regions, parse, and return tuples.'''

  File sources (loaded from disk):
    def load(path: Path) -> list[tuple[int, str, str, list[str]]]:
        '''Load a local file into (number, name, category, aliases) tuples.'''

The source is selected by config keys `provider` and `provider_source`.
If `provider_source` is a file path (has a file extension), the framework
resolves it by extension to a shared source module (e.g. .csv → sources/csv.py)
and calls its `load(path)` interface.
Otherwise it's treated as a web source module name, resolved via:
sources/<provider>/<source>.py then sources/<source>.py, and called via `fetch()`.

Callers should use `fetch_source()` instead of `get_source()` directly -
it handles the fetch/load branching and returns channel tuples.
"""
from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any


_SOURCES_DIR = Path(__file__).parent


def available_sources(provider_name: str = "") -> list[str]:
    """List available source names.

    If provider_name is given, lists sources in that provider's subdirectory.
    Otherwise lists shared (top-level) sources.
    """
    search_dir = _SOURCES_DIR / provider_name if provider_name else _SOURCES_DIR
    if not search_dir.is_dir():
        return []
    return sorted(
        p.stem for p in search_dir.glob("*.py")
        if p.stem != "__init__" and not p.stem.startswith("_")
    )


def get_source(provider_name: str, source_name: str) -> Any:
    """Import and return a source module.

    If source_name has a file extension (e.g. "channels.csv"), resolves by
    extension to a shared source module (e.g. .csv → sources/csv.py).
    Otherwise looks in sources/<provider>/ first, then falls back to sources/ (shared).
    """
    # File path - resolve by extension
    source_path = Path(source_name)
    if source_path.suffix:
        ext = source_path.suffix.lstrip(".")
        ext_module_path = _SOURCES_DIR / f"{ext}.py"
        if ext_module_path.exists():
            return importlib.import_module(f"sources.{ext}")
        raise ValueError(
            f"No source module for extension '.{ext}' (from '{source_name}'). "
            f"Expected: sources/{ext}.py"
        )

    # Provider-specific source
    if provider_name:
        provider_path = _SOURCES_DIR / provider_name / f"{source_name}.py"
        if provider_path.exists():
            return importlib.import_module(f"sources.{provider_name}.{source_name}")

    # Shared source
    shared_path = _SOURCES_DIR / f"{source_name}.py"
    if shared_path.exists() and source_name != "__init__":
        return importlib.import_module(f"sources.{source_name}")

    # Not found - build helpful error
    provider_sources = available_sources(provider_name)
    shared_sources = available_sources()
    all_sources = sorted(set(provider_sources + shared_sources))
    raise ValueError(
        f"Unknown source '{source_name}' for provider '{provider_name}'. "
        f"Available: {', '.join(all_sources) if all_sources else '(none)'}"
    )


def fetch_source(
    provider_name: str,
    source_name: str,
    **kwargs: Any,
) -> list[tuple[int, str, str, list[str]]]:
    """Resolve a source module and fetch channel data.

    Handles the fetch() vs load() branching so callers don't need to know
    which interface the source implements:

    - If source_name has a file extension (e.g. "channels.csv"), resolves the
      module by extension and calls load(path) with the source_name as the path.
    - Otherwise resolves a web source module and calls fetch(**kwargs).
    """
    module = get_source(provider_name, source_name)

    source_path = Path(source_name)
    if source_path.suffix:
        # File source - call load(path)
        if not hasattr(module, "load"):
            raise ValueError(
                f"Source module '{module.__name__}' has no load() function. "
                f"File sources must implement: def load(path: Path) -> list[tuple]"
            )
        return module.load(source_path)

    # Web source - call fetch(**kwargs)
    if not hasattr(module, "fetch"):
        raise ValueError(
            f"Source module '{module.__name__}' has no fetch() function. "
            f"Web sources must implement: def fetch(**kwargs) -> list[tuple]"
        )
    return module.fetch(**kwargs)
