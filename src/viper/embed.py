from __future__ import annotations
from dataclasses import dataclass, field
from pathlib import Path
from viper.freezer import FrozenModule


@dataclass
class GeneratedSources:
    """files produced by the code generator that need to be compiled."""
    # standalone binary sources
    bin_c: Path
    bin_extra: list[Path] = field(default_factory=list)
    # importable module sources
    mod_c: Path | None = None
    mod_extra: list[Path] = field(default_factory=list)


def generate_all(
    modules: list[FrozenModule],
    entry_point: str,
    out_dir: Path,
    package_name: str | None = None,
    package_version: str | None = None,
    standalone_bundle: str | None = None,
    python_version: str | None = None,
    has_native_packages: bool = False,
    top_level_name: str = "",
) -> GeneratedSources:
    """generate C + binary blob for both the standalone binary and importable .so.

    writes to out_dir:
      viper_blob.bin   - raw concatenated bytecode
      viper_blob.S     - assembly that embeds the blob via .incbin
      viper_main.c     - standalone binary (main + frozen table)
      viper_module.c   - importable extension module (PyInit + frozen merge)
    """
    blob_path = out_dir / "viper_blob.bin"
    asm_path = out_dir / "viper_blob.S"

    # write bytecode blob and compute offsets
    offsets: list[tuple[str, int, int, bool]] = []
    with open(blob_path, "wb") as f:
        for mod in modules:
            offset = f.tell()
            f.write(mod.bytecode)
            offsets.append((mod.name, offset, len(mod.bytecode), mod.is_package))

    _write_asm(asm_path, blob_path)

    # shared frozen table declaration (used by both C files)
    frozen_table = _build_frozen_table(offsets)

    # standalone binary
    bin_c = out_dir / "viper_main.c"
    bin_c.write_text(_generate_binary_c(
        frozen_table, entry_point, package_name, package_version,
        standalone_bundle, python_version, has_native_packages,
    ))

    # importable module
    mod_c = out_dir / "viper_module.c"
    mod_c.write_text(_generate_module_c(
        frozen_table, offsets, top_level_name or _guess_top_level(modules),
    ))

    return GeneratedSources(
        bin_c=bin_c,
        bin_extra=[asm_path],
        mod_c=mod_c,
        mod_extra=[asm_path],
    )


# -- shared helpers --

def _build_frozen_table(offsets: list[tuple[str, int, int, bool]]) -> str:
    """generate the struct _frozen array source pointing into the blob."""
    lines = []
    lines.append('extern const unsigned char viper_blob[];')
    lines.append('')
    lines.append('static const struct _frozen viper_frozen_modules[] = {')
    for name, offset, size, is_pkg in offsets:
        pkg = 1 if is_pkg else 0
        lines.append(f'    {{"{name}", viper_blob + {offset}, {size}, {pkg}}},')
    lines.append('    {NULL, NULL, 0, 0}')
    lines.append('};')
    return "\n".join(lines)


def _write_asm(asm_path: Path, blob_path: Path) -> None:
    abs_blob = str(blob_path.resolve())
    lines = [
        '#ifdef __APPLE__',
        '.section __DATA,__const',
        '#else',
        '.section .rodata',
        '#endif',
        '.globl _viper_blob',
        '.globl viper_blob',
        '.p2align 4',
        '_viper_blob:',
        'viper_blob:',
        f'.incbin "{abs_blob}"',
    ]
    asm_path.write_text("\n".join(lines) + "\n")


def _guess_top_level(modules: list[FrozenModule]) -> str:
    """guess the top-level package name from the module list."""
    for mod in modules:
        if mod.is_package and "." not in mod.name:
            return mod.name
    if modules:
        return modules[0].name.split(".")[0]
    return "unknown"


def _c_ident(dotted_name: str) -> str:
    return dotted_name.replace(".", "__").replace("-", "_")


def _parse_entry_point(entry_point: str) -> tuple[str, str]:
    if ":" not in entry_point:
        return entry_point, "main"
    mod, call = entry_point.rsplit(":", 1)
    call = call.rstrip("()")
    return mod, call


# -- binary C generation --

def _generate_binary_c(
    frozen_table: str,
    entry_point: str,
    package_name: str | None,
    package_version: str | None,
    standalone_bundle: str | None,
    python_version: str | None,
    has_native_packages: bool,
) -> str:
    lines: list[str] = []
    lines.append('#include <Python.h>')
    lines.append('#include <limits.h>')
    lines.append('#include <libgen.h>')
    lines.append('#ifdef __APPLE__')
    lines.append('#include <mach-o/dyld.h>')
    lines.append('#endif')
    lines.append('')
    lines.append(frozen_table)
    lines.append('')

    startup_py = _generate_startup_python(package_name, package_version)
    if startup_py:
        escaped = startup_py.replace("\\", "\\\\").replace('"', '\\"').replace("\n", '\\n"\n"')
        lines.append(f'static const char *viper_startup_code = "{escaped}";')
        lines.append('')

    _emit_exe_dir_helper(lines)

    mod_part, callable_part = _parse_entry_point(entry_point)

    lines.append('int main(int argc, char **argv) {')
    lines.append('    PyImport_FrozenModules = viper_frozen_modules;')
    lines.append('')
    lines.append('    char exe_dir[PATH_MAX];')
    lines.append('    int have_exe_dir = (get_exe_dir(exe_dir, sizeof(exe_dir)) == 0);')
    lines.append('')
    lines.append('    PyStatus status;')
    lines.append('    PyConfig config;')
    lines.append('    PyConfig_InitPythonConfig(&config);')
    lines.append('    config.install_signal_handlers = 1;')
    lines.append('    config.parse_argv = 0;')
    lines.append('')
    lines.append('    status = PyConfig_SetBytesArgv(&config, argc, argv);')
    lines.append('    if (PyStatus_Exception(status)) goto fail;')
    lines.append('')

    if standalone_bundle:
        pyver = python_version or "3.14"
        pyver_nodot = pyver.replace(".", "")
        _emit_standalone_path_config(lines, standalone_bundle, pyver, pyver_nodot,
                                     has_native_packages)

    lines.append('    status = Py_InitializeFromConfig(&config);')
    lines.append('    if (PyStatus_Exception(status)) goto fail;')
    lines.append('    PyConfig_Clear(&config);')
    lines.append('')

    if has_native_packages and standalone_bundle:
        lines.append('    if (have_exe_dir) {')
        lines.append('        char sp_path[PATH_MAX];')
        lines.append(f'        snprintf(sp_path, sizeof(sp_path), "%s/{standalone_bundle}/site-packages", exe_dir);')
        lines.append('        PyObject *sys_path = PySys_GetObject("path");')
        lines.append('        PyList_Insert(sys_path, 0, PyUnicode_FromString(sp_path));')
        lines.append('    }')
        lines.append('')

    if startup_py:
        lines.append('    if (PyRun_SimpleString(viper_startup_code) != 0) {')
        lines.append('        fprintf(stderr, "viper: startup code failed\\n");')
        lines.append('    }')
        lines.append('')

    lines.append(f'    PyObject *entry_mod = PyImport_ImportModule("{mod_part}");')
    lines.append('    if (entry_mod == NULL) { PyErr_Print(); Py_Finalize(); return 1; }')
    lines.append('')
    lines.append(f'    PyObject *callable = PyObject_GetAttrString(entry_mod, "{callable_part}");')
    lines.append('    if (callable == NULL) { PyErr_Print(); Py_DECREF(entry_mod); Py_Finalize(); return 1; }')
    lines.append('')
    lines.append('    PyObject *result = PyObject_CallNoArgs(callable);')
    lines.append('    if (result == NULL) {')
    lines.append('        if (PyErr_ExceptionMatches(PyExc_SystemExit)) {')
    lines.append('            PyObject *exc = PyErr_GetRaisedException();')
    lines.append('            PyObject *code_obj = PyObject_GetAttrString(exc, "code");')
    lines.append('            int exit_code = 0;')
    lines.append('            if (code_obj && PyLong_Check(code_obj)) exit_code = (int)PyLong_AsLong(code_obj);')
    lines.append('            Py_XDECREF(code_obj); Py_DECREF(exc);')
    lines.append('            Py_DECREF(callable); Py_DECREF(entry_mod);')
    lines.append('            Py_Finalize(); return exit_code;')
    lines.append('        }')
    lines.append('        PyErr_Print();')
    lines.append('        Py_DECREF(callable); Py_DECREF(entry_mod);')
    lines.append('        Py_Finalize(); return 1;')
    lines.append('    }')
    lines.append('')
    lines.append('    Py_DECREF(result); Py_DECREF(callable); Py_DECREF(entry_mod);')
    lines.append('    Py_Finalize(); return 0;')
    lines.append('')
    lines.append('fail:')
    lines.append('    PyConfig_Clear(&config);')
    lines.append('    Py_ExitStatusException(status);')
    lines.append('    return 1;')
    lines.append('}')

    return "\n".join(lines) + "\n"


# -- module C generation --

def _generate_module_c(
    frozen_table: str,
    offsets: list[tuple[str, int, int, bool]],
    top_level: str,
) -> str:
    """generate a C extension module that merges frozen modules into the interpreter.

    on import, PyInit_<name> merges our frozen table so submodule imports
    work, then manually unmarshals and executes the top-level package
    bytecode (can't use PyImport_ImportFrozenModule during PyInit).
    """
    c_top = _c_ident(top_level)
    n_modules = len(offsets)

    # find the top-level module in the offsets
    top_offset = 0
    top_size = 0
    top_is_pkg = False
    for name, offset, size, is_pkg in offsets:
        if name == top_level:
            top_offset = offset
            top_size = size
            top_is_pkg = is_pkg
            break

    lines: list[str] = []
    lines.append('#include <Python.h>')
    lines.append('#include <marshal.h>')
    lines.append('#include <string.h>')
    lines.append('#include <stdlib.h>')
    lines.append('')
    lines.append(frozen_table)
    lines.append('')

    lines.append('static struct _frozen *merged_frozen = NULL;')
    lines.append('')
    lines.append('static void merge_frozen_tables(void) {')
    lines.append('    if (merged_frozen) return;')
    lines.append('    int n_old = 0;')
    lines.append('    const struct _frozen *p = PyImport_FrozenModules;')
    lines.append('    while (p && p->name) { n_old++; p++; }')
    lines.append(f'    int n_new = {n_modules};')
    lines.append('    merged_frozen = malloc((n_old + n_new + 1) * sizeof(struct _frozen));')
    lines.append('    if (!merged_frozen) return;')
    lines.append('    memcpy(merged_frozen, PyImport_FrozenModules, n_old * sizeof(struct _frozen));')
    lines.append('    memcpy(merged_frozen + n_old, viper_frozen_modules, n_new * sizeof(struct _frozen));')
    lines.append('    memset(&merged_frozen[n_old + n_new], 0, sizeof(struct _frozen));')
    lines.append('    PyImport_FrozenModules = merged_frozen;')
    lines.append('}')
    lines.append('')

    lines.append(f'static PyModuleDef viper_module_def = {{')
    lines.append(f'    PyModuleDef_HEAD_INIT, "{top_level}", NULL, -1, NULL,')
    lines.append(f'}};')
    lines.append('')

    lines.append(f'PyMODINIT_FUNC PyInit_{c_top}(void) {{')
    lines.append('    merge_frozen_tables();')
    lines.append('')
    lines.append(f'    PyObject *mod = PyModule_Create(&viper_module_def);')
    lines.append('    if (!mod) return NULL;')
    lines.append('')

    if top_is_pkg:
        lines.append('    /* mark as package so submodule imports work */')
        lines.append('    PyObject *path = PyList_New(0);')
        lines.append('    PyModule_AddObject(mod, "__path__", path);')
        lines.append('')

    lines.append(f'    PyObject *code = PyMarshal_ReadObjectFromString(')
    lines.append(f'        (const char *)(viper_blob + {top_offset}), {top_size});')
    lines.append('    if (!code) { Py_DECREF(mod); return NULL; }')
    lines.append('')
    lines.append('    PyObject *d = PyModule_GetDict(mod);')
    lines.append('    PyObject *r = PyEval_EvalCode(code, d, d);')
    lines.append('    Py_DECREF(code);')
    lines.append('    if (!r) { Py_DECREF(mod); return NULL; }')
    lines.append('    Py_DECREF(r);')
    lines.append('')
    lines.append('    return mod;')
    lines.append('}')

    return "\n".join(lines) + "\n"


# -- helpers --

def _emit_exe_dir_helper(lines: list[str]) -> None:
    lines.append('static int get_exe_dir(char *buf, size_t bufsize) {')
    lines.append('#ifdef __APPLE__')
    lines.append('    uint32_t size = (uint32_t)bufsize;')
    lines.append('    if (_NSGetExecutablePath(buf, &size) != 0) return -1;')
    lines.append('    char resolved[PATH_MAX];')
    lines.append('    if (!realpath(buf, resolved)) return -1;')
    lines.append('    char *dir = dirname(resolved);')
    lines.append('    strncpy(buf, dir, bufsize - 1);')
    lines.append('    buf[bufsize - 1] = 0;')
    lines.append('    return 0;')
    lines.append('#else')
    lines.append('    ssize_t len = readlink("/proc/self/exe", buf, bufsize - 1);')
    lines.append('    if (len < 0) return -1;')
    lines.append('    buf[len] = 0;')
    lines.append('    char *dir = dirname(buf);')
    lines.append('    if (dir != buf) memmove(buf, dir, strlen(dir) + 1);')
    lines.append('    return 0;')
    lines.append('#endif')
    lines.append('}')
    lines.append('')


def _emit_standalone_path_config(
    lines: list[str],
    bundle_name: str,
    pyver: str,
    pyver_nodot: str,
    has_native_packages: bool,
) -> None:
    lines.append('    if (have_exe_dir) {')
    lines.append('        char home_path[PATH_MAX];')
    lines.append(f'        snprintf(home_path, sizeof(home_path), "%s/{bundle_name}", exe_dir);')
    lines.append('        wchar_t whome[PATH_MAX];')
    lines.append('        mbstowcs(whome, home_path, PATH_MAX);')
    lines.append('        status = PyConfig_SetString(&config, &config.home, whome);')
    lines.append('        if (PyStatus_Exception(status)) goto fail;')
    lines.append('')
    lines.append('        config.module_search_paths_set = 1;')
    lines.append('        char path_buf[PATH_MAX];')
    lines.append('        wchar_t wpath[PATH_MAX];')
    lines.append('')
    lines.append(f'        snprintf(path_buf, sizeof(path_buf), "%s/{bundle_name}/python{pyver_nodot}.zip", exe_dir);')
    lines.append('        mbstowcs(wpath, path_buf, PATH_MAX);')
    lines.append('        status = PyWideStringList_Append(&config.module_search_paths, wpath);')
    lines.append('        if (PyStatus_Exception(status)) goto fail;')
    lines.append('')
    lines.append(f'        snprintf(path_buf, sizeof(path_buf), "%s/{bundle_name}/python{pyver}/lib-dynload", exe_dir);')
    lines.append('        mbstowcs(wpath, path_buf, PATH_MAX);')
    lines.append('        status = PyWideStringList_Append(&config.module_search_paths, wpath);')
    lines.append('        if (PyStatus_Exception(status)) goto fail;')

    if has_native_packages:
        lines.append('')
        lines.append(f'        snprintf(path_buf, sizeof(path_buf), "%s/{bundle_name}/site-packages", exe_dir);')
        lines.append('        mbstowcs(wpath, path_buf, PATH_MAX);')
        lines.append('        status = PyWideStringList_Append(&config.module_search_paths, wpath);')
        lines.append('        if (PyStatus_Exception(status)) goto fail;')

    lines.append('    }')
    lines.append('')


def _generate_startup_python(
    package_name: str | None = None,
    package_version: str | None = None,
) -> str:
    parts = ["import sys", "sys.frozen = True"]

    if package_name and package_version:
        norm_name = package_name.replace("-", "_").lower()
        parts.append(f'''
try:
    import importlib.metadata as _md
    import email.message
    class _VD(_md.Distribution):
        def read_text(self, f):
            return "Name: {package_name}\\nVersion: {package_version}\\n" if f == "METADATA" else None
        def locate_file(self, p):
            return p
    class _VF(_md.DistributionFinder):
        def find_distributions(self, context=_md.DistributionFinder.Context()):
            n = context.name
            if n and n.replace("-", "_").lower() in ("{norm_name}", "{package_name.lower()}"):
                return [_VD()]
            return [_VD()] if not n else []
    sys.meta_path.append(_VF())
except Exception:
    pass
''')

    return "\n".join(parts)
