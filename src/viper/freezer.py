from __future__ import annotations
import base64
import json
import marshal
import importlib
import tomllib
import importlib.metadata
import importlib.util
import os
import re
import shutil
import subprocess
import sys
import sysconfig
from collections import deque
from pathlib import Path
from dataclasses import dataclass, field


# toolchain python for cross-compiling bytecode. set via set_cross_python()
# before scanning. when None, uses the host python's compile() directly.
_cross_python: Path | None = None
_cross_proc: subprocess.Popen | None = None

# helper script run in the toolchain python. reads file paths from stdin,
# writes back base64-encoded marshalled bytecode over stdout.
_COMPILER_SCRIPT = r"""
import sys, marshal, base64
for line in sys.stdin:
    path = line.strip()
    if not path:
        continue
    try:
        with open(path) as f:
            source = f.read()
        code = compile(source, path, "exec", dont_inherit=True, optimize=2)
        data = base64.b64encode(marshal.dumps(code)).decode()
        sys.stdout.write(f"OK {data}\n")
    except Exception as e:
        sys.stdout.write(f"ERR {e}\n")
    sys.stdout.flush()
"""


def set_cross_python(python_path: Path | None) -> None:
    """set the toolchain python used for bytecode compilation."""
    global _cross_python, _cross_proc
    _cross_python = python_path
    if _cross_proc is not None:
        _cross_proc.terminate()
        _cross_proc = None


def _get_cross_proc() -> subprocess.Popen:
    """get or start the persistent cross-compiler subprocess."""
    global _cross_proc
    if _cross_proc is None or _cross_proc.poll() is not None:
        _cross_proc = subprocess.Popen(
            [str(_cross_python), "-c", _COMPILER_SCRIPT],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
        )
    return _cross_proc


@dataclass
class FrozenModule:
    """a python module compiled to bytecode and ready to embed in C"""
    name: str           # dotted module name (e.g. "runpod_flash.endpoint")
    is_package: bool    # true if this is an __init__.py
    bytecode: bytes     # marshalled code object
    source_path: Path   # original .py file


@dataclass
class PackageScan:
    """result of scanning a python package and its dependencies"""
    modules: list[FrozenModule] = field(default_factory=list)
    entry_point: str = ""
    c_extensions: list[Path] = field(default_factory=list)


@dataclass
class NativePackage:
    """a package or file with C extensions that must be copied, not frozen"""
    source: Path       # directory or .so file to copy
    relative: str      # path relative to site-packages root


@dataclass
class DependencyScan:
    """result of scanning all transitive dependencies"""
    frozen: list[FrozenModule] = field(default_factory=list)
    native: list[NativePackage] = field(default_factory=list)


def scan_package(package_dir: Path, entry_point: str = "") -> PackageScan:
    """scan a python package directory and collect all .py files as frozen modules."""
    result = PackageScan(entry_point=entry_point)
    src_dir = _find_src_dir(package_dir)

    pkg_dirs = [d for d in src_dir.iterdir() if d.is_dir() and (d / "__init__.py").exists()]
    if not pkg_dirs:
        raise ValueError(f"no python package found in {src_dir}")

    for pkg_dir in pkg_dirs:
        pkg_name = pkg_dir.name
        _collect_modules(pkg_dir, pkg_name, result)

    return result


def scan_all_dependencies(package_dir: Path) -> DependencyScan:
    """scan all transitive dependencies and categorize them.

    pure python packages are compiled to bytecode for freezing.
    packages with C extensions (.so) or data files are collected for copying.
    dist-info directories are always bundled so importlib.metadata works.
    also scans source for undeclared imports (e.g. extras-gated deps).
    """
    from viper.dep_scanner import find_undeclared_deps

    result = DependencyScan()
    direct_deps = _get_dependencies(package_dir)
    all_dist_names = _resolve_transitive_deps(direct_deps)

    # scan source for imports not covered by declared deps
    src_dir = _find_src_dir(package_dir)
    source_files = list(src_dir.rglob("*.py"))
    undeclared = find_undeclared_deps(source_files, all_dist_names)
    if undeclared:
        extra_transitive = _resolve_transitive_deps(list(undeclared))
        all_dist_names |= extra_transitive

    sp_dirs = _get_site_packages_dirs()

    for dist_name in sorted(all_dist_names):
        try:
            dist = _find_distribution(dist_name)
        except Exception:
            continue
        if dist is None:
            continue

        # bundle the .dist-info directory so importlib.metadata works
        _bundle_dist_info(dist, sp_dirs, result)

        top_level_items = _get_top_level_items(dist)

        for item_name, item_path, item_type in top_level_items:
            if item_type == "package":
                if _package_needs_bundling(item_path):
                    sp_root = item_path.parent
                    result.native.append(NativePackage(
                        source=item_path,
                        relative=item_name,
                    ))
                    # also collect standalone .so files (e.g. mypyc support)
                    if dist.files:
                        for f in dist.files:
                            s = str(f)
                            if s.endswith(".so") and "/" not in s:
                                so_path = sp_root / s
                                if so_path.exists():
                                    result.native.append(NativePackage(
                                        source=so_path,
                                        relative=s,
                                    ))
                else:
                    _collect_installed_modules(item_path, item_name, result.frozen)

            elif item_type == "module":
                try:
                    bytecode = compile_to_bytecode(item_path)
                    result.frozen.append(FrozenModule(
                        name=item_name,
                        is_package=False,
                        bytecode=bytecode,
                        source_path=item_path,
                    ))
                except Exception:
                    pass

            elif item_type == "so":
                result.native.append(NativePackage(
                    source=item_path,
                    relative=item_path.name,
                ))

    return result


def _bundle_dist_info(dist, sp_dirs: list[Path], result: DependencyScan) -> None:
    """find and bundle the .dist-info directory for a distribution."""
    if not dist.files:
        return
    # the first file's parent with .dist-info suffix is the dist-info dir
    for f in dist.files:
        parts = str(f).split("/")
        if parts and parts[0].endswith(".dist-info"):
            for sp in sp_dirs:
                info_dir = sp / parts[0]
                if info_dir.is_dir():
                    result.native.append(NativePackage(
                        source=info_dir,
                        relative=parts[0],
                    ))
                    return
            break


def scan_stdlib_subset(used_modules: set[str]) -> list[FrozenModule]:
    """collect stdlib modules that the package actually imports."""
    modules = []
    stdlib_dir = Path(sysconfig.get_path("stdlib"))

    for mod_name in sorted(used_modules):
        frozen = _try_freeze_stdlib_module(mod_name, stdlib_dir)
        if frozen:
            modules.extend(frozen)

    return modules


def compile_to_bytecode(source_path: Path) -> bytes:
    """compile a .py file to marshalled bytecode.

    uses the cross-compiler subprocess when set_cross_python() has been
    called, otherwise compiles with the host python directly.
    """
    if _cross_python is not None:
        return _cross_compile(source_path)
    with open(source_path, "r") as f:
        source = f.read()
    code = compile(source, str(source_path), "exec", dont_inherit=True, optimize=2)
    return marshal.dumps(code)


def _cross_compile(source_path: Path) -> bytes:
    """compile a single file via the toolchain python subprocess."""
    proc = _get_cross_proc()
    proc.stdin.write(f"{source_path}\n")
    proc.stdin.flush()
    line = proc.stdout.readline().strip()
    if line.startswith("OK "):
        return base64.b64decode(line[3:])
    elif line.startswith("ERR "):
        raise SyntaxError(f"cross-compile failed for {source_path}: {line[4:]}")
    else:
        raise RuntimeError(f"unexpected cross-compiler response: {line!r}")


# --- dependency resolution ---

def _resolve_transitive_deps(direct_deps: list[str]) -> set[str]:
    """resolve the full transitive closure of dependencies."""
    seen: set[str] = set()
    queue = deque(direct_deps)

    while queue:
        dep = queue.popleft()
        norm = _normalize_dist_name(dep)
        if norm in seen:
            continue
        seen.add(norm)

        try:
            dist = _find_distribution(dep)
        except Exception:
            continue
        if dist is None:
            continue

        reqs = dist.requires or []
        for r in reqs:
            # skip extras-only and conditional deps
            if ";" in r:
                marker_str = r.split(";", 1)[1].strip()
                # skip deps gated on extra
                if "extra" in marker_str:
                    continue
            name = re.split(r"[;<>=!~\[\s]", r)[0].strip()
            if name:
                queue.append(name)

    return seen


def _find_distribution(name: str):
    """find a distribution by name, trying common normalizations."""
    normalized = _normalize_dist_name(name)
    for variant in [name, normalized, normalized.replace("-", "_"),
                    normalized.replace("_", "-")]:
        try:
            return importlib.metadata.distribution(variant)
        except importlib.metadata.PackageNotFoundError:
            continue
    return None


def _get_top_level_items(dist) -> list[tuple[str, Path, str]]:
    """get the top-level importable items from a distribution.

    returns list of (name, path, type) where type is "package", "module", or "so".
    """
    items = []
    sp_dirs = _get_site_packages_dirs()

    # try top_level.txt first
    tl_text = dist.read_text("top_level.txt")
    if tl_text:
        for line in tl_text.strip().splitlines():
            name = line.strip()
            if not name or name.startswith(".") or name == "tests":
                continue
            for sp in sp_dirs:
                pkg_dir = sp / name
                if pkg_dir.is_dir() and (pkg_dir / "__init__.py").exists():
                    items.append((name, pkg_dir, "package"))
                    break
                py_file = sp / f"{name}.py"
                if py_file.is_file():
                    items.append((name, py_file, "module"))
                    break
                # check for standalone .so
                for so in sp.glob(f"{name}.cpython-*.so"):
                    items.append((name, so, "so"))
                    break
                for so in sp.glob(f"{name}.abi3.so"):
                    items.append((name, so, "so"))
                    break

    # fallback: scan dist files
    if not items and dist.files:
        seen_tops: set[str] = set()
        for f in dist.files:
            parts = str(f).split("/")
            if not parts or parts[0].endswith((".dist-info", ".data", "__pycache__")):
                continue
            top = parts[0]
            if top in seen_tops or top == "tests":
                continue

            for sp in sp_dirs:
                if len(parts) >= 2 and parts[-1] == "__init__.py" and top == parts[0]:
                    pkg_dir = sp / top
                    if pkg_dir.is_dir():
                        seen_tops.add(top)
                        items.append((top, pkg_dir, "package"))
                        break
                elif len(parts) == 1 and top.endswith(".py"):
                    mod_name = top[:-3]
                    py_file = sp / top
                    if py_file.is_file():
                        seen_tops.add(top)
                        items.append((mod_name, py_file, "module"))
                        break
                elif len(parts) == 1 and top.endswith(".so"):
                    so_file = sp / top
                    if so_file.is_file():
                        seen_tops.add(top)
                        mod_name = top.split(".")[0]
                        items.append((mod_name, so_file, "so"))
                        break

    return items


def _get_site_packages_dirs() -> list[Path]:
    """get all site-packages directories from the current environment."""
    dirs = []
    for p in sys.path:
        pp = Path(p)
        if pp.is_dir() and "site-packages" in str(pp):
            dirs.append(pp)
    if not dirs:
        import site
        for sp in site.getsitepackages():
            dirs.append(Path(sp))
    return dirs


# --- package scanning ---

def _find_src_dir(package_dir: Path) -> Path:
    """find the source root (handles src/ layout)."""
    src = package_dir / "src"
    if src.is_dir():
        return src
    return package_dir


def _collect_modules(directory: Path, prefix: str, result: PackageScan) -> None:
    """recursively collect .py files from a directory."""
    init_path = directory / "__init__.py"
    if init_path.exists():
        bytecode = compile_to_bytecode(init_path)
        result.modules.append(FrozenModule(
            name=prefix,
            is_package=True,
            bytecode=bytecode,
            source_path=init_path,
        ))

    for item in sorted(directory.iterdir()):
        if item.is_file() and item.suffix == ".py" and item.name != "__init__.py":
            mod_name = f"{prefix}.{item.stem}"
            bytecode = compile_to_bytecode(item)
            result.modules.append(FrozenModule(
                name=mod_name,
                is_package=False,
                bytecode=bytecode,
                source_path=item,
            ))
        elif item.is_dir() and (item / "__init__.py").exists():
            sub_prefix = f"{prefix}.{item.name}"
            _collect_modules(item, sub_prefix, result)
        elif item.is_file() and item.suffix == ".so":
            result.c_extensions.append(item)


def _collect_installed_modules(
    directory: Path, prefix: str, modules: list[FrozenModule]
) -> None:
    """recursively collect .py files from an installed package directory."""
    init_path = directory / "__init__.py"
    if init_path.exists():
        try:
            bytecode = compile_to_bytecode(init_path)
            modules.append(FrozenModule(
                name=prefix,
                is_package=True,
                bytecode=bytecode,
                source_path=init_path,
            ))
        except Exception:
            pass

    for item in sorted(directory.iterdir()):
        if item.name == "__pycache__":
            continue
        if item.is_file() and item.suffix == ".py" and item.name != "__init__.py":
            mod_name = f"{prefix}.{item.stem}"
            try:
                bytecode = compile_to_bytecode(item)
                modules.append(FrozenModule(
                    name=mod_name,
                    is_package=False,
                    bytecode=bytecode,
                    source_path=item,
                ))
            except Exception:
                pass
        elif item.is_dir() and (item / "__init__.py").exists():
            sub_prefix = f"{prefix}.{item.name}"
            _collect_installed_modules(item, sub_prefix, modules)


def _dir_has_so(directory: Path) -> bool:
    """check if a directory tree contains any .so files."""
    for _ in directory.rglob("*.so"):
        return True
    return False


# files that are safe to ignore when deciding if a package has runtime data
_CODE_SUFFIXES = frozenset({
    ".py", ".pyc", ".pyi", ".pyx", ".pxd", ".pxi", ".c", ".h",
})
_IGNORABLE_NAMES = frozenset({
    "__pycache__", "py.typed", ".gitignore",
})


def _package_needs_bundling(directory: Path) -> bool:
    """check if a package has .so files or data files needed at runtime.

    packages that contain C extensions or non-code files (certificates, json
    configs, templates, etc.) must be copied to the bundle rather than frozen,
    because frozen modules can't serve files via importlib.resources, and
    frozen packages have __path__ = [] so .so submodules can't be found.
    """
    for item in directory.rglob("*"):
        if not item.is_file():
            continue
        if item.name in _IGNORABLE_NAMES:
            continue
        if any(p == "__pycache__" for p in item.parts):
            continue
        if item.suffix not in _CODE_SUFFIXES:
            return True
    return False


# --- pyproject.toml parsing ---

def _load_pyproject(package_dir: Path) -> dict | None:
    """load and parse pyproject.toml from a package directory."""
    pyproject = package_dir / "pyproject.toml"
    if not pyproject.exists():
        return None
    with open(pyproject, "rb") as f:
        return tomllib.load(f)


def _get_dependencies(package_dir: Path) -> list[str]:
    """extract dependency names from pyproject.toml."""
    data = _load_pyproject(package_dir)
    if data is None:
        return []
    raw_deps = data.get("project", {}).get("dependencies", [])
    deps = []
    for spec in raw_deps:
        name = re.split(r"[;<>=!~\[\s]", spec)[0].strip()
        if name:
            deps.append(name)
    return deps


def _normalize_dist_name(name: str) -> str:
    """normalize a distribution name for comparison."""
    return re.sub(r"[-_.]+", "-", name).lower()


# --- stdlib freezing ---

def _try_freeze_stdlib_module(mod_name: str, stdlib_dir: Path) -> list[FrozenModule] | None:
    """try to find and freeze a stdlib module."""
    spec = importlib.util.find_spec(mod_name)
    if spec is None:
        return None

    if spec.origin and spec.origin.endswith(".py"):
        origin = Path(spec.origin)
        if origin.exists():
            try:
                bytecode = compile_to_bytecode(origin)
                is_pkg = spec.submodule_search_locations is not None
                result = [FrozenModule(
                    name=mod_name,
                    is_package=is_pkg,
                    bytecode=bytecode,
                    source_path=origin,
                )]
                if is_pkg and spec.submodule_search_locations:
                    for loc in spec.submodule_search_locations:
                        loc_path = Path(loc)
                        if loc_path.is_dir():
                            _collect_installed_modules(loc_path, mod_name, result)
                return result
            except Exception:
                return None

    return None
