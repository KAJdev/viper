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


def _detect_python_version(tc_dir: Path) -> str:
    """detect the python major.minor version from a toolchain directory."""
    for d in (tc_dir / "lib").iterdir():
        if d.is_dir() and d.name.startswith("python3."):
            return d.name.replace("python", "")
    raise RuntimeError(f"could not detect python version in {tc_dir}")


def compile_c_files(
    sources: list[Path],
    output: Path,
    config: CompilerConfig | None = None,
    mode: str = "binary",
    extra_objects: list[Path] | None = None,
    tc_dir: Path | None = None,
    python_version: str | None = None,
) -> Path:
    """compile C source files into a binary or shared library.

    mode: "binary" for standalone executable, "module" for .so python extension
    tc_dir: path to the python-build-standalone installation
    python_version: minor version (e.g. "3.14") for path/lib resolution
    """
    if config is None:
        config = CompilerConfig()

    if mode == "binary":
        return _compile_binary(sources, output, config, extra_objects, tc_dir, python_version)
    elif mode == "module":
        return _compile_module(sources, output, config, extra_objects)
    else:
        raise ValueError(f"unknown mode: {mode}")


def _compile_binary(
    sources: list[Path],
    output: Path,
    config: CompilerConfig,
    extra_objects: list[Path] | None = None,
    tc_dir: Path | None = None,
    python_version: str | None = None,
) -> Path:
    """compile a standalone binary using python-build-standalone.

    produces a self-contained binary bundle (binary + dylib + stdlib).
    falls back to dynamic linking against the current python if tc_dir
    is not provided.
    """
    if tc_dir:
        return _compile_binary_standalone(sources, output, config, tc_dir, python_version, extra_objects)
    else:
        return _compile_binary_dynamic(sources, output, config, extra_objects)


def _compile_binary_standalone(
    sources: list[Path],
    output: Path,
    config: CompilerConfig,
    tc_dir: Path,
    python_version: str | None = None,
    extra_objects: list[Path] | None = None,
) -> Path:
    """compile a standalone binary bundle using python-build-standalone.

    produces:
      output           - the executable
      output.lib/      - bundled python dylib + zipped stdlib
    """
    from viper.toolchain import get_python_include, get_python_dylib, get_python_stdlib

    pyver = python_version or _detect_python_version(tc_dir)
    include_dir = get_python_include(tc_dir, pyver)
    dylib_path = get_python_dylib(tc_dir, pyver)
    stdlib_dir = get_python_stdlib(tc_dir, pyver)

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
    cmd += [f"-L{tc_dir / 'lib'}"]
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
    stdlib_zip = lib_dir / f"python{pyver.replace('.', '')}.zip"
    _create_stdlib_zip(stdlib_dir, stdlib_zip, tc_dir=tc_dir)

    # copy lib-dynload .so files (the few not built into the dylib)
    dynload_src = stdlib_dir / "lib-dynload"
    if dynload_src.exists():
        dynload_dst = lib_dir / f"python{pyver}" / "lib-dynload"
        dynload_dst.mkdir(parents=True, exist_ok=True)
        for so in dynload_src.glob("*.so"):
            shutil.copy2(so, dynload_dst / so.name)

    return output


def _create_stdlib_zip(stdlib_dir: Path, output_zip: Path, tc_dir: Path | None = None) -> None:
    """create a zip archive of the python stdlib with compiled bytecode.

    uses the toolchain python to compile .pyc files so the bytecode matches
    the target python version. this is the main startup optimization for
    the standalone build -- without .pyc, every stdlib import compiles from
    source at startup.
    """
    skip_dirs = {"test", "tests", "idle_test", "tkinter",
                 "turtledemo", "ensurepip", "lib2to3", "lib-dynload",
                 "site-packages"}

    # collect all .py files first
    py_files: list[tuple[Path, str]] = []
    for root, dirs, files in os.walk(stdlib_dir):
        rel_root = Path(root).relative_to(stdlib_dir)
        dirs[:] = [d for d in dirs if d not in skip_dirs
                   and not d.startswith("config-")
                   and d != "__pycache__"]
        for f in files:
            if f.endswith(".py"):
                py_files.append((Path(root) / f, str(rel_root / f)))

    # batch-compile .pyc using the toolchain python
    pyc_data = _batch_compile_stdlib(py_files, tc_dir)

    with zipfile.ZipFile(output_zip, "w", zipfile.ZIP_DEFLATED) as zf:
        for src, arcname in py_files:
            zf.write(src, arcname)
            pyc = pyc_data.get(arcname)
            if pyc:
                zf.writestr(arcname + "c", pyc)


def _batch_compile_stdlib(
    py_files: list[tuple[Path, str]],
    tc_dir: Path | None,
) -> dict[str, bytes]:
    """compile stdlib .py files to .pyc using the toolchain python.

    runs a single subprocess that compiles all files and writes .pyc data
    to a temp directory. returns a dict of arcname -> pyc bytes.
    """
    import base64
    import tempfile

    if tc_dir is None:
        return {}

    from viper.toolchain import get_python_bin
    tc_python = get_python_bin(tc_dir)

    # helper script: reads (arcname, filepath) pairs from stdin,
    # compiles each, writes base64-encoded .pyc to stdout
    script = r"""
import sys, marshal, struct, importlib.util, base64
magic = importlib.util.MAGIC_NUMBER
flags = b'\x00\x00\x00\x00'
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    arcname, filepath = line.split('\t', 1)
    try:
        with open(filepath, 'rb') as f:
            source = f.read()
        code = compile(source, arcname, 'exec', dont_inherit=True, optimize=0)
        data = marshal.dumps(code)
        mtime = int(__import__('os').path.getmtime(filepath))
        size = len(source)
        header = magic + flags + struct.pack('<II', mtime, size)
        encoded = base64.b64encode(header + data).decode()
        sys.stdout.write(f'OK\t{arcname}\t{encoded}\n')
    except Exception as e:
        sys.stdout.write(f'ERR\t{arcname}\t{e}\n')
    sys.stdout.flush()
"""

    proc = subprocess.Popen(
        [str(tc_python), "-c", script],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )

    # feed all files
    input_lines = []
    for src, arcname in py_files:
        input_lines.append(f"{arcname}\t{src}")
    stdout, _ = proc.communicate("\n".join(input_lines) + "\n")

    result: dict[str, bytes] = {}
    for line in stdout.splitlines():
        parts = line.split("\t", 2)
        if len(parts) == 3 and parts[0] == "OK":
            result[parts[1]] = base64.b64decode(parts[2])

    return result


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
