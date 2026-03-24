from __future__ import annotations
import argparse
import os
import shutil
import sys
import tempfile
import tomllib
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="viper",
        description="python to native code compiler",
    )
    sub = parser.add_subparsers(dest="command")

    build_p = sub.add_parser("build", help="compile a python package")
    build_p.add_argument("path", type=Path, help="path to python package or .py file")
    build_p.add_argument("-o", "--output", type=Path, default=None, help="output path")
    build_p.add_argument("--binary", action="store_true", default=True,
                         help="produce standalone binary (default)")
    build_p.add_argument("--module", action="store_true",
                         help="produce importable python C extension")
    build_p.add_argument("--cdll", action="store_true",
                         help="produce shared library + ctypes stub")
    build_p.add_argument("--entry-point", type=str, default=None,
                         help="entry point (module:callable), auto-detected from pyproject.toml")
    build_p.add_argument("--include-deps", action="store_true", default=True,
                         help="bundle third-party dependencies (default: true)")
    build_p.add_argument("--no-deps", action="store_true",
                         help="skip bundling third-party dependencies")
    build_p.add_argument("-v", "--verbose", action="store_true")

    args = parser.parse_args(argv)

    if args.command is None:
        parser.print_help()
        return 1

    if args.command == "build":
        return cmd_build(args)

    return 0


def cmd_build(args: argparse.Namespace) -> int:
    from viper.freezer import scan_package, scan_all_dependencies, NativePackage
    from viper.embed import generate_frozen_c
    from viper.linker import compile_c_files, CompilerConfig, get_standalone_python_dir, \
        _detect_python_version, bundle_native_packages

    pkg_path = args.path.resolve()
    if not pkg_path.exists():
        print(f"error: {pkg_path} does not exist", file=sys.stderr)
        return 1

    verbose = args.verbose

    # detect entry point
    entry_point = args.entry_point
    if entry_point is None:
        entry_point = _detect_entry_point(pkg_path)
    if entry_point is None:
        print("error: could not detect entry point. use --entry-point module:callable",
              file=sys.stderr)
        return 1

    if verbose:
        print(f"entry point: {entry_point}")

    # scan the main package
    if verbose:
        print(f"scanning package: {pkg_path}")
    scan = scan_package(pkg_path, entry_point)
    if verbose:
        print(f"  found {len(scan.modules)} modules")

    all_modules = list(scan.modules)
    native_packages: list[NativePackage] = []

    # scan and freeze all transitive dependencies
    if not args.no_deps:
        if verbose:
            print("scanning dependencies...")
        dep_scan = scan_all_dependencies(pkg_path)
        all_modules.extend(dep_scan.frozen)
        native_packages = dep_scan.native
        if verbose:
            print(f"  frozen: {len(dep_scan.frozen)} modules")
            print(f"  native: {len(dep_scan.native)} packages (have C extensions)")

    if verbose:
        print(f"total modules to embed: {len(all_modules)}")

    # determine output name
    pkg_name = _detect_package_name(pkg_path)
    if args.output:
        output = args.output.resolve()
    else:
        script_name = _detect_script_name(pkg_path)
        output = Path.cwd() / (script_name or pkg_name)

    # detect standalone mode
    pbs_dir = get_standalone_python_dir()
    standalone_bundle = None
    python_version = None
    if pbs_dir:
        standalone_bundle = f"{output.name}.lib"
        python_version = _detect_python_version(pbs_dir)

    # generate and compile
    with tempfile.TemporaryDirectory(prefix="viper_") as tmpdir:
        tmp = Path(tmpdir)
        c_file = tmp / "viper_embedded.c"

        pkg_version = _detect_package_version(pkg_path)

        if verbose:
            print(f"generating C source ({len(all_modules)} modules)...")

        generate_frozen_c(
            all_modules, entry_point, c_file,
            package_name=_detect_dist_name(pkg_path) or pkg_name,
            package_version=pkg_version,
            standalone_bundle=standalone_bundle,
            python_version=python_version,
            has_native_packages=bool(native_packages),
        )

        if verbose:
            c_size = c_file.stat().st_size
            print(f"  generated {c_size:,} bytes of C")

        config = CompilerConfig()
        if verbose:
            print(f"compiling with {config.cc}...")

        compile_c_files(
            sources=[c_file],
            output=output,
            config=config,
            mode="binary",
        )

    # bundle native packages (C extensions) alongside the binary
    if native_packages and standalone_bundle:
        sp_dir = output.parent / standalone_bundle / "site-packages"
        bundle_native_packages(native_packages, sp_dir, verbose=verbose)

    size = output.stat().st_size
    print(f"built: {output} ({size:,} bytes)")
    return 0


def _load_pyproject(pkg_path: Path) -> dict | None:
    """load and parse pyproject.toml from a package directory."""
    pyproject = pkg_path / "pyproject.toml"
    if not pyproject.exists():
        return None
    with open(pyproject, "rb") as f:
        return tomllib.load(f)


def _detect_entry_point(pkg_path: Path) -> str | None:
    """try to detect the entry point from pyproject.toml."""
    data = _load_pyproject(pkg_path)
    if data is None:
        if pkg_path.is_file() and pkg_path.suffix == ".py":
            return f"{pkg_path.stem}:main"
        return None

    scripts = data.get("project", {}).get("scripts", {})
    if scripts:
        return next(iter(scripts.values()))
    return None


def _detect_package_name(pkg_path: Path) -> str:
    """derive a package name from pyproject.toml."""
    data = _load_pyproject(pkg_path)
    if data:
        name = data.get("project", {}).get("name")
        if name:
            return name.replace("-", "_")

    if pkg_path.is_file():
        return pkg_path.stem
    return pkg_path.name


def _detect_dist_name(pkg_path: Path) -> str | None:
    """get the distribution name (with hyphens) from pyproject.toml."""
    data = _load_pyproject(pkg_path)
    if data:
        return data.get("project", {}).get("name")
    return None


def _detect_script_name(pkg_path: Path) -> str | None:
    """get the CLI script name from [project.scripts] in pyproject.toml."""
    data = _load_pyproject(pkg_path)
    if data:
        scripts = data.get("project", {}).get("scripts", {})
        if scripts:
            return next(iter(scripts.keys()))
    return None


def _detect_package_version(pkg_path: Path) -> str | None:
    """extract the version from pyproject.toml."""
    data = _load_pyproject(pkg_path)
    if data:
        return data.get("project", {}).get("version")
    return None


if __name__ == "__main__":
    sys.exit(main())
