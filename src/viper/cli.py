from __future__ import annotations
import argparse
import sys
import tempfile
import tomllib
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="viperc",
        description="compile python packages to standalone binaries",
    )
    sub = parser.add_subparsers(dest="command")

    build_p = sub.add_parser("build", help="compile a python package to a binary")
    build_p.add_argument("path", type=Path, help="path to python package directory")
    build_p.add_argument("-o", "--output", type=Path, default=None, help="output binary path")
    build_p.add_argument("--entry-point", type=str, default=None,
                         help="entry point (module:callable), auto-detected from pyproject.toml")
    build_p.add_argument("--python", type=str, default=None,
                         help="python version to target (e.g. 3.12, 3.14). "
                              "defaults to 3.14")
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
    from viper.toolchain import (
        ensure_toolchain, get_python_bin, DEFAULT_PYTHON,
    )
    from viper.resolver import install_deps
    from viper.freezer import (
        scan_package, scan_all_dependencies, set_cross_python, NativePackage,
    )
    from viper.embed import generate_frozen_c
    from viper.linker import compile_c_files, CompilerConfig, bundle_native_packages

    pkg_path = args.path.resolve()
    if not pkg_path.exists():
        print(f"error: {pkg_path} does not exist", file=sys.stderr)
        return 1

    verbose = args.verbose
    pyproject = _load_pyproject(pkg_path)
    python_version = args.python or DEFAULT_PYTHON

    # detect entry point
    entry_point = args.entry_point or _get_entry_point(pyproject, pkg_path)
    if entry_point is None:
        print("error: could not detect entry point. use --entry-point module:callable",
              file=sys.stderr)
        return 1

    if verbose:
        print(f"entry point: {entry_point}")
        print(f"target python: {python_version}")

    # ensure toolchain is available for the requested python version
    tc_dir = ensure_toolchain(python_version=python_version, verbose=verbose)
    tc_python = get_python_bin(tc_dir)

    # cross-compile bytecode using the toolchain python
    set_cross_python(tc_python)

    try:
        return _do_build(args, pkg_path, pyproject, python_version,
                         tc_dir, tc_python, verbose)
    finally:
        set_cross_python(None)


def _do_build(
    args: argparse.Namespace,
    pkg_path: Path,
    pyproject: dict | None,
    python_version: str,
    tc_dir: Path,
    tc_python: Path,
    verbose: bool,
) -> int:
    from viper.resolver import install_deps
    from viper.freezer import scan_package, scan_all_dependencies, NativePackage
    from viper.embed import generate_frozen_c
    from viper.linker import compile_c_files, CompilerConfig, bundle_native_packages

    # normalize to minor version for paths/flags
    pyver = _to_minor(python_version)

    # install target's deps into a temp directory
    with tempfile.TemporaryDirectory(prefix="viper_deps_") as deps_dir:
        if not args.no_deps and pyproject:
            raw_deps = pyproject.get("project", {}).get("dependencies", [])
            install_deps(raw_deps, Path(deps_dir) / "site-packages",
                         tc_python=tc_python, verbose=verbose)

        # scan and freeze
        if verbose:
            print(f"scanning package: {pkg_path}")
        scan = scan_package(pkg_path, entry_point=args.entry_point or _get_entry_point(pyproject, pkg_path))
        if verbose:
            print(f"  found {len(scan.modules)} modules")

        all_modules = list(scan.modules)
        native_packages: list[NativePackage] = []

        if not args.no_deps:
            if verbose:
                print("scanning dependencies...")
            dep_scan = scan_all_dependencies(pkg_path)
            all_modules.extend(dep_scan.frozen)
            native_packages = dep_scan.native
            if verbose:
                print(f"  frozen: {len(dep_scan.frozen)} modules")
                print(f"  native: {len(dep_scan.native)} packages (C extensions / data files)")

        if verbose:
            print(f"total modules to embed: {len(all_modules)}")

        # determine output path
        pkg_name = _get_package_name(pyproject, pkg_path)
        if args.output:
            output = args.output.resolve()
        else:
            script_name = _get_script_name(pyproject)
            output = Path.cwd() / (script_name or pkg_name)

        bundle_name = f"{output.name}.lib"

        # generate C and compile
        with tempfile.TemporaryDirectory(prefix="viper_cc_") as cc_dir:
            c_file = Path(cc_dir) / "viper_embedded.c"

            if verbose:
                print(f"generating C source ({len(all_modules)} modules)...")

            gen = generate_frozen_c(
                all_modules, entry_point=args.entry_point or _get_entry_point(pyproject, pkg_path),
                output=c_file,
                package_name=_get_dist_name(pyproject) or pkg_name,
                package_version=_get_version(pyproject),
                standalone_bundle=bundle_name,
                python_version=pyver,
                has_native_packages=bool(native_packages),
            )

            if verbose:
                c_size = gen.c_file.stat().st_size
                blob_size = (gen.c_file.parent / "viper_blob.bin").stat().st_size
                print(f"  C source: {c_size:,} bytes, bytecode blob: {blob_size:,} bytes")
                print("compiling...")

            compile_c_files(
                sources=[gen.c_file] + gen.extra_objects,
                output=output,
                config=CompilerConfig(),
                mode="binary",
                tc_dir=tc_dir,
                python_version=pyver,
            )

        # bundle native packages
        if native_packages:
            sp_dir = output.parent / bundle_name / "site-packages"
            bundle_native_packages(native_packages, sp_dir, verbose=verbose)

    size = output.stat().st_size
    lib_dir = output.parent / bundle_name
    if lib_dir.exists():
        import shutil
        lib_size = sum(f.stat().st_size for f in lib_dir.rglob("*") if f.is_file())
        print(f"built: {output} ({size:,} bytes, bundle {lib_size:,} bytes)")
    else:
        print(f"built: {output} ({size:,} bytes)")
    return 0


def _to_minor(version: str) -> str:
    """normalize to major.minor (e.g. "3.14.3" -> "3.14")."""
    parts = version.split(".")
    if len(parts) >= 2:
        return f"{parts[0]}.{parts[1]}"
    return version


# --- pyproject.toml helpers ---

def _load_pyproject(pkg_path: Path) -> dict | None:
    pyproject = pkg_path / "pyproject.toml"
    if not pyproject.exists():
        return None
    with open(pyproject, "rb") as f:
        return tomllib.load(f)


def _get_entry_point(data: dict | None, pkg_path: Path) -> str | None:
    if data:
        scripts = data.get("project", {}).get("scripts", {})
        if scripts:
            return next(iter(scripts.values()))
    if pkg_path.is_file() and pkg_path.suffix == ".py":
        return f"{pkg_path.stem}:main"
    return None


def _get_package_name(data: dict | None, pkg_path: Path) -> str:
    if data:
        name = data.get("project", {}).get("name")
        if name:
            return name.replace("-", "_")
    if pkg_path.is_file():
        return pkg_path.stem
    return pkg_path.name


def _get_dist_name(data: dict | None) -> str | None:
    if data:
        return data.get("project", {}).get("name")
    return None


def _get_script_name(data: dict | None) -> str | None:
    if data:
        scripts = data.get("project", {}).get("scripts", {})
        if scripts:
            return next(iter(scripts.keys()))
    return None


def _get_version(data: dict | None) -> str | None:
    if data:
        return data.get("project", {}).get("version")
    return None


if __name__ == "__main__":
    sys.exit(main())
