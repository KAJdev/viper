from __future__ import annotations
import shutil
import subprocess
import sys
import sysconfig
import zipfile
import os
from pathlib import Path
from dataclasses import dataclass, field


@dataclass
class CompilerConfig:
    """configuration for the C compiler and linker"""
    cc: str = "cc"
    opt_level: str = "-O2"
    # paths derived from the active python environment (used for module mode)
    python_include: str = field(default_factory=lambda: sysconfig.get_path("include"))
    python_libdir: str = field(default_factory=lambda: sysconfig.get_config_var("LIBDIR"))
    python_ldversion: str = field(default_factory=lambda: sysconfig.get_config_var("LDVERSION") or "")
    python_framework: str = field(default_factory=lambda: sysconfig.get_config_var("PYTHONFRAMEWORK") or "")
    ext_suffix: str = field(default_factory=lambda: sysconfig.get_config_var("EXT_SUFFIX") or ".so")

    @property
    def python_lib_name(self) -> str:
        return f"python{self.python_ldversion}"


def get_runtime_dir() -> Path:
    """path to the viper C runtime sources"""
    return Path(__file__).parent.parent.parent / "runtime"


def get_standalone_python_dir() -> Path | None:
    """path to the python-build-standalone install_only distribution."""
    viper_root = Path(__file__).parent.parent.parent
    pbs_dir = viper_root / ".python-standalone" / "python"
    if pbs_dir.exists() and (pbs_dir / "lib").exists():
        return pbs_dir
    return None


def compile_c_files(
    sources: list[Path],
    output: Path,
    config: CompilerConfig | None = None,
    mode: str = "binary",
    extra_objects: list[Path] | None = None,
) -> Path:
    """compile C source files into a binary or shared library.

    mode: "binary" for standalone executable, "module" for .so python extension
    """
    if config is None:
        config = CompilerConfig()

    if mode == "binary":
        return _compile_binary(sources, output, config, extra_objects)
    elif mode == "module":
        return _compile_module(sources, output, config, extra_objects)
    else:
        raise ValueError(f"unknown mode: {mode}")


def _compile_binary(
    sources: list[Path],
    output: Path,
    config: CompilerConfig,
    extra_objects: list[Path] | None = None,
) -> Path:
    """compile a standalone binary.

    uses python-build-standalone if available to produce a self-contained
    binary bundle (binary + dylib + stdlib). falls back to dynamic linking
    against the current python otherwise.
    """
    pbs_dir = get_standalone_python_dir()

    if pbs_dir:
        return _compile_binary_standalone(sources, output, config, pbs_dir, extra_objects)
    else:
        return _compile_binary_dynamic(sources, output, config, extra_objects)


def _detect_python_version(pbs_dir: Path) -> str:
    """detect the python major.minor version from the standalone build."""
    for d in (pbs_dir / "lib").iterdir():
        if d.is_dir() and d.name.startswith("python3."):
            return d.name.replace("python", "")
    raise RuntimeError("could not detect python version in standalone build")


def _compile_binary_standalone(
    sources: list[Path],
    output: Path,
    config: CompilerConfig,
    pbs_dir: Path,
    extra_objects: list[Path] | None = None,
) -> Path:
    """compile a standalone binary bundle using python-build-standalone.

    produces:
      output           - the executable
      output.lib/      - bundled python dylib + zipped stdlib
    """
    pyver = _detect_python_version(pbs_dir)
    include_dir = pbs_dir / "include" / f"python{pyver}"
    dylib_path = pbs_dir / "lib" / f"libpython{pyver}.dylib"

    if not dylib_path.exists():
        raise RuntimeError(f"python dylib not found: {dylib_path}")

    # create the lib bundle directory
    lib_dir = output.parent / f"{output.name}.lib"
    lib_dir.mkdir(parents=True, exist_ok=True)

    # compile, linking against the standalone dylib
    cmd = [config.cc, config.opt_level]
    cmd += [f"-I{include_dir}"]
    cmd += [f"-I{get_runtime_dir()}"]

    cmd += [str(s) for s in sources]

    if extra_objects:
        cmd += [str(o) for o in extra_objects]

    # link against libpython, with rpath pointing to the bundle dir
    cmd += [f"-L{pbs_dir / 'lib'}"]
    cmd += [f"-lpython{pyver}"]
    cmd += [f"-Wl,-rpath,@executable_path/{output.name}.lib"]
    cmd += ["-ldl", "-lm"]
    if sys.platform == "darwin":
        cmd += ["-framework", "CoreFoundation"]

    cmd += ["-o", str(output)]

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"compilation failed:\n{result.stderr}")

    # copy the python dylib to the bundle directory
    shutil.copy2(dylib_path, lib_dir / dylib_path.name)

    # create a zipped stdlib for the bundle
    stdlib_dir = pbs_dir / "lib" / f"python{pyver}"
    stdlib_zip = lib_dir / f"python{pyver.replace('.', '')}.zip"
    _create_stdlib_zip(stdlib_dir, stdlib_zip)

    # copy lib-dynload .so files (the few not built into the dylib)
    dynload_src = stdlib_dir / "lib-dynload"
    if dynload_src.exists():
        dynload_dst = lib_dir / f"python{pyver}" / "lib-dynload"
        dynload_dst.mkdir(parents=True, exist_ok=True)
        for so in dynload_src.glob("*.so"):
            shutil.copy2(so, dynload_dst / so.name)

    return output


def _create_stdlib_zip(stdlib_dir: Path, output_zip: Path) -> None:
    """create a zip archive of the python stdlib with compiled bytecode.

    includes .pyc files alongside .py so zipimport doesn't need to
    compile source at import time. this is the main startup optimization
    for the standalone build.
    """
    import marshal
    import struct
    import importlib.util
    import time

    skip_dirs = {"test", "tests", "idle_test", "tkinter",
                 "turtledemo", "ensurepip", "lib2to3", "lib-dynload",
                 "site-packages"}

    magic = importlib.util.MAGIC_NUMBER
    flags = b'\x00\x00\x00\x00'

    with zipfile.ZipFile(output_zip, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, dirs, files in os.walk(stdlib_dir):
            rel_root = Path(root).relative_to(stdlib_dir)

            dirs[:] = [d for d in dirs if d not in skip_dirs
                       and not d.startswith("config-")
                       and d != "__pycache__"]

            for f in files:
                if not f.endswith(".py"):
                    continue
                src = Path(root) / f
                arcname = str(rel_root / f)

                # write .py source
                zf.write(src, arcname)

                # compile to .pyc and write alongside
                try:
                    source = src.read_bytes()
                    code = compile(source, arcname, "exec", dont_inherit=True, optimize=0)
                    data = marshal.dumps(code)
                    # .pyc header: magic(4) + flags(4) + mtime(4) + size(4)
                    mtime = int(src.stat().st_mtime)
                    size = len(source)
                    header = magic + flags + struct.pack("<II", mtime, size)
                    pyc_arcname = arcname + "c"  # foo.py -> foo.pyc
                    zf.writestr(pyc_arcname, header + data)
                except Exception:
                    pass


def _compile_binary_dynamic(
    sources: list[Path],
    output: Path,
    config: CompilerConfig,
    extra_objects: list[Path] | None = None,
) -> Path:
    """compile a binary dynamically linked against the current python (fallback)."""
    cmd = [config.cc, config.opt_level]
    cmd += [f"-I{config.python_include}"]
    cmd += [f"-I{get_runtime_dir()}"]

    cmd += [str(s) for s in sources]

    if extra_objects:
        cmd += [str(o) for o in extra_objects]

    if sys.platform == "darwin" and config.python_framework:
        framework_prefix = sysconfig.get_config_var("PYTHONFRAMEWORKPREFIX") or ""
        if framework_prefix:
            cmd += [f"-F{framework_prefix}"]
        cmd += ["-framework", config.python_framework]
    else:
        cmd += [f"-L{config.python_libdir}", f"-l{config.python_lib_name}"]

    cmd += ["-ldl", "-lm"]
    if sys.platform == "darwin":
        cmd += ["-framework", "CoreFoundation"]

    cmd += ["-o", str(output)]

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"compilation failed:\n{result.stderr}")

    return output


def _compile_module(
    sources: list[Path],
    output: Path,
    config: CompilerConfig,
    extra_objects: list[Path] | None = None,
) -> Path:
    """compile a python C extension module (.so)."""
    cmd = [config.cc, config.opt_level]
    cmd += ["-shared", "-fPIC"]
    cmd += [f"-I{config.python_include}"]
    cmd += [f"-I{get_runtime_dir()}"]

    cmd += [str(s) for s in sources]

    if extra_objects:
        cmd += [str(o) for o in extra_objects]

    cmd += ["-o", str(output)]

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"module compilation failed:\n{result.stderr}")

    return output


def bundle_native_packages(
    native_packages: list,
    dest_dir: Path,
    verbose: bool = False,
) -> None:
    """copy native packages (with C extensions) to the bundle directory.

    preserves directory structure so python can find .so files via sys.path.
    """
    from viper.freezer import NativePackage

    dest_dir.mkdir(parents=True, exist_ok=True)
    seen: set[str] = set()

    for pkg in native_packages:
        if pkg.relative in seen:
            continue
        seen.add(pkg.relative)

        dst = dest_dir / pkg.relative
        if pkg.source.is_dir():
            if dst.exists():
                shutil.rmtree(dst)
            shutil.copytree(pkg.source, dst, symlinks=True)
            if verbose:
                so_count = sum(1 for _ in dst.rglob("*.so"))
                print(f"  bundled {pkg.relative}/ ({so_count} .so files)")
        elif pkg.source.is_file():
            shutil.copy2(pkg.source, dst)
            if verbose:
                print(f"  bundled {pkg.relative}")
