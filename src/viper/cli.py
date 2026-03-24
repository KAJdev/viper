from __future__ import annotations
import argparse
import os
import shutil
import sys
import tempfile
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
    from viper.freezer import scan_package, scan_dependencies, scan_stdlib_subset
    from viper.embed import generate_frozen_c
    from viper.linker import compile_c_files, CompilerConfig
    from viper.dep_scanner import find_all_imports

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

    # for binary mode, we freeze only the main package modules.
    # third-party deps and stdlib are loaded from the python environment
    # at runtime via site-packages. this avoids version mismatches and
    # handles C extensions (pydantic-core, etc) that can't be frozen.
    #
    # future: --standalone flag will bundle everything for fully
    # self-contained distribution.

    if verbose:
        print(f"total modules to embed: {len(all_modules)}")

    # determine output name
    if args.output:
        output = args.output.resolve()
    else:
        # prefer the script name from [project.scripts] for binary output
        script_name = _detect_script_name(pkg_path)
        if script_name:
            output = Path.cwd() / script_name
        else:
            pkg_name = _detect_package_name(pkg_path)
            output = Path.cwd() / pkg_name

    # generate and compile
    with tempfile.TemporaryDirectory(prefix="viper_") as tmpdir:
        tmp = Path(tmpdir)

        # generate the C source with embedded bytecode
        c_file = tmp / "viper_embedded.c"
        if verbose:
            print(f"generating C source ({len(all_modules)} modules)...")

        # collect site-packages paths from the current environment
        import site
        site_packages = site.getsitepackages()
        # also include user site and any PYTHONPATH entries
        user_site = site.getusersitepackages()
        if isinstance(user_site, str):
            site_packages.append(user_site)
        # include paths from sys.path that look like site-packages or source dirs
        for p in sys.path:
            if p and p not in site_packages and os.path.isdir(p):
                site_packages.append(p)

        # detect package name and version for metadata
        pkg_name = _detect_package_name(pkg_path)
        pkg_version = _detect_package_version(pkg_path)

        # detect standalone mode (python-build-standalone available)
        from viper.linker import get_standalone_python_dir, _detect_python_version
        pbs_dir = get_standalone_python_dir()
        standalone_bundle = None
        python_version = None
        if pbs_dir:
            script_name = _detect_script_name(pkg_path)
            bin_name = script_name or pkg_name
            standalone_bundle = f"{bin_name}.lib"
            python_version = _detect_python_version(pbs_dir)

        generate_frozen_c(
            all_modules, entry_point, c_file,
            site_packages=site_packages,
            package_name=_detect_dist_name(pkg_path) or pkg_name,
            package_version=pkg_version,
            standalone_bundle=standalone_bundle,
            python_version=python_version,
        )

        if verbose:
            c_size = c_file.stat().st_size
            print(f"  generated {c_size:,} bytes of C")

        # compile
        config = CompilerConfig()
        if verbose:
            print(f"compiling with {config.cc}...")

        compile_c_files(
            sources=[c_file],
            output=output,
            config=config,
            mode="binary",
        )

    size = output.stat().st_size
    print(f"built: {output} ({size:,} bytes)")
    return 0


def _detect_entry_point(pkg_path: Path) -> str | None:
    """try to detect the entry point from pyproject.toml."""
    pyproject = pkg_path / "pyproject.toml"
    if not pyproject.exists():
        # single file mode
        if pkg_path.is_file() and pkg_path.suffix == ".py":
            return f"{pkg_path.stem}:main"
        return None

    text = pyproject.read_text()
    # look for [project.scripts]
    in_scripts = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped == "[project.scripts]":
            in_scripts = True
            continue
        if in_scripts:
            if stripped.startswith("["):
                break
            if "=" in stripped:
                # take the first script entry
                _, value = stripped.split("=", 1)
                value = value.strip().strip('"').strip("'")
                return value

    return None


def _detect_package_name(pkg_path: Path) -> str:
    """derive a binary name from the package."""
    pyproject = pkg_path / "pyproject.toml"
    if pyproject.exists():
        text = pyproject.read_text()
        for line in text.splitlines():
            if line.strip().startswith("name"):
                _, value = line.split("=", 1)
                name = value.strip().strip('"').strip("'")
                return name.replace("-", "_")

    if pkg_path.is_file():
        return pkg_path.stem

    return pkg_path.name


def _detect_dist_name(pkg_path: Path) -> str | None:
    """get the distribution name (with hyphens) from pyproject.toml."""
    pyproject = pkg_path / "pyproject.toml"
    if pyproject.exists():
        text = pyproject.read_text()
        for line in text.splitlines():
            if line.strip().startswith("name"):
                _, value = line.split("=", 1)
                return value.strip().strip('"').strip("'")
    return None


def _detect_script_name(pkg_path: Path) -> str | None:
    """get the CLI script name from [project.scripts] in pyproject.toml."""
    pyproject = pkg_path / "pyproject.toml"
    if not pyproject.exists():
        return None
    text = pyproject.read_text()
    in_scripts = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped == "[project.scripts]":
            in_scripts = True
            continue
        if in_scripts:
            if stripped.startswith("["):
                break
            if "=" in stripped:
                name, _ = stripped.split("=", 1)
                return name.strip()
    return None


def _detect_package_version(pkg_path: Path) -> str | None:
    """extract the version from pyproject.toml."""
    pyproject = pkg_path / "pyproject.toml"
    if not pyproject.exists():
        return None
    text = pyproject.read_text()
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("version") and "=" in stripped:
            _, value = stripped.split("=", 1)
            return value.strip().strip('"').strip("'")
    return None


if __name__ == "__main__":
    sys.exit(main())
