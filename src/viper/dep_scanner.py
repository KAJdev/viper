from __future__ import annotations
import ast
import importlib.metadata
import sys
from pathlib import Path


BUILTIN_MODULES = frozenset(sys.builtin_module_names)


def find_source_imports(source_paths: list[Path]) -> set[str]:
    """scan source files and collect all top-level imported module names."""
    top_imports: set[str] = set()

    for path in source_paths:
        try:
            source = path.read_text()
            tree = ast.parse(source, str(path))
        except Exception:
            continue

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    top_imports.add(alias.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    top_imports.add(node.module.split(".")[0])

    return top_imports


def find_undeclared_deps(source_paths: list[Path], declared_deps: set[str]) -> set[str]:
    """find third-party packages imported by source but not in the declared dep list.

    catches undeclared dependencies and extras-gated transitive deps
    by checking what's actually imported against what's installed.
    """
    imported = find_source_imports(source_paths)
    extra_deps: set[str] = set()

    for mod_name in imported:
        if mod_name in BUILTIN_MODULES:
            continue
        if mod_name in sys.stdlib_module_names:
            continue
        if _normalize(mod_name) in declared_deps:
            continue

        # check if it's an installed third-party package
        dist = _find_dist_for_module(mod_name)
        if dist:
            extra_deps.add(dist)

    return extra_deps


def _find_dist_for_module(mod_name: str) -> str | None:
    """find the distribution name that provides a given top-level module."""
    # try direct name match first
    for variant in [mod_name, mod_name.replace("_", "-")]:
        try:
            importlib.metadata.distribution(variant)
            return variant
        except importlib.metadata.PackageNotFoundError:
            pass

    # search through installed packages' top_level.txt
    for dist in importlib.metadata.distributions():
        tl = dist.read_text("top_level.txt")
        if tl:
            tops = {line.strip() for line in tl.splitlines() if line.strip()}
            if mod_name in tops:
                return dist.name
    return None


def _normalize(name: str) -> str:
    import re
    return re.sub(r"[-_.]+", "-", name).lower()
