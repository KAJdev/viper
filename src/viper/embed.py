from __future__ import annotations
from pathlib import Path
from viper.freezer import FrozenModule


def generate_frozen_c(
    modules: list[FrozenModule],
    entry_point: str,
    output: Path,
    package_name: str | None = None,
    package_version: str | None = None,
    standalone_bundle: str | None = None,
    python_version: str | None = None,
    has_native_packages: bool = False,
) -> Path:
    """generate a C file that embeds all frozen modules using PyImport_FrozenModules.

    uses cpython's native frozen module infrastructure so imports are resolved
    entirely in C with no python-level finder overhead.

    standalone_bundle: the name of the .lib directory next to the binary
        (e.g. "flash.lib"). the generated C configures python to find its
        stdlib and native packages from this bundle at runtime.
    has_native_packages: if true, adds the bundle's site-packages dir to
        sys.path so C extensions can be found.
    """
    lines: list[str] = []
    lines.append('#include <Python.h>')
    lines.append('#include <limits.h>')
    lines.append('#include <libgen.h>')
    lines.append('#ifdef __APPLE__')
    lines.append('#include <mach-o/dyld.h>')
    lines.append('#endif')
    lines.append('')

    # emit bytecode arrays
    for mod in modules:
        c_name = _c_ident(mod.name)
        data = mod.bytecode
        lines.append(f'static const unsigned char frozen_{c_name}[] = {{')
        for i in range(0, len(data), 16):
            chunk = data[i:i+16]
            hex_vals = ", ".join(f"0x{b:02x}" for b in chunk)
            lines.append(f'    {hex_vals},')
        lines.append('};')
        lines.append('')

    # emit the frozen module table (struct _frozen array)
    lines.append('static const struct _frozen viper_frozen_modules[] = {')
    for mod in modules:
        c_name = _c_ident(mod.name)
        pkg = 1 if mod.is_package else 0
        lines.append(f'    {{"{mod.name}", frozen_{c_name}, '
                     f'(int)sizeof(frozen_{c_name}), {pkg}}},')
    lines.append('    {NULL, NULL, 0, 0}')
    lines.append('};')
    lines.append('')

    # minimal startup python code (metadata injection + sys.frozen)
    startup_py = _generate_startup_python(
        package_name=package_name,
        package_version=package_version,
    )
    if startup_py:
        escaped = startup_py.replace("\\", "\\\\").replace('"', '\\"').replace("\n", '\\n"\n"')
        lines.append(f'static const char *viper_startup_code = "{escaped}";')
        lines.append('')

    _emit_exe_dir_helper(lines)

    # emit main
    mod_part, callable_part = _parse_entry_point(entry_point)

    lines.append('int main(int argc, char **argv) {')
    lines.append('    /* register frozen modules before interpreter init */')
    lines.append('    PyImport_FrozenModules = viper_frozen_modules;')
    lines.append('')
    lines.append('    /* resolve path to this executable */')
    lines.append('    char exe_dir[PATH_MAX];')
    lines.append('    int have_exe_dir = (get_exe_dir(exe_dir, sizeof(exe_dir)) == 0);')
    lines.append('')
    lines.append('    PyStatus status;')
    lines.append('    PyConfig config;')
    lines.append('    PyConfig_InitPythonConfig(&config);')
    lines.append('')
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

    # add bundle site-packages to sys.path for C extension loading
    if has_native_packages and standalone_bundle:
        lines.append('    /* add bundle site-packages to sys.path for C extensions */')
        lines.append('    if (have_exe_dir) {')
        lines.append('        char sp_path[PATH_MAX];')
        lines.append(f'        snprintf(sp_path, sizeof(sp_path), "%s/{standalone_bundle}/site-packages", exe_dir);')
        lines.append('        PyObject *sys_path = PySys_GetObject("path");')
        lines.append('        PyList_Insert(sys_path, 0, PyUnicode_FromString(sp_path));')
        lines.append('    }')
        lines.append('')

    # run startup code (sys.frozen + metadata)
    if startup_py:
        lines.append('    /* set sys.frozen and inject package metadata */')
        lines.append('    if (PyRun_SimpleString(viper_startup_code) != 0) {')
        lines.append('        fprintf(stderr, "viper: startup code failed\\n");')
        lines.append('    }')
        lines.append('')

    # call entry point
    lines.append(f'    /* run entry point: {entry_point} */')
    lines.append(f'    PyObject *entry_mod = PyImport_ImportModule("{mod_part}");')
    lines.append('    if (entry_mod == NULL) {')
    lines.append('        PyErr_Print();')
    lines.append('        Py_Finalize();')
    lines.append('        return 1;')
    lines.append('    }')
    lines.append('')
    lines.append(f'    PyObject *callable = PyObject_GetAttrString(entry_mod, "{callable_part}");')
    lines.append('    if (callable == NULL) {')
    lines.append('        PyErr_Print();')
    lines.append('        Py_DECREF(entry_mod);')
    lines.append('        Py_Finalize();')
    lines.append('        return 1;')
    lines.append('    }')
    lines.append('')
    lines.append('    PyObject *result = PyObject_CallNoArgs(callable);')
    lines.append('    if (result == NULL) {')
    lines.append('        if (PyErr_ExceptionMatches(PyExc_SystemExit)) {')
    lines.append('            PyObject *exc = PyErr_GetRaisedException();')
    lines.append('            PyObject *code_obj = PyObject_GetAttrString(exc, "code");')
    lines.append('            int exit_code = 0;')
    lines.append('            if (code_obj && PyLong_Check(code_obj))')
    lines.append('                exit_code = (int)PyLong_AsLong(code_obj);')
    lines.append('            Py_XDECREF(code_obj);')
    lines.append('            Py_DECREF(exc);')
    lines.append('            Py_DECREF(callable);')
    lines.append('            Py_DECREF(entry_mod);')
    lines.append('            Py_Finalize();')
    lines.append('            return exit_code;')
    lines.append('        }')
    lines.append('        PyErr_Print();')
    lines.append('        Py_DECREF(callable);')
    lines.append('        Py_DECREF(entry_mod);')
    lines.append('        Py_Finalize();')
    lines.append('        return 1;')
    lines.append('    }')
    lines.append('')
    lines.append('    Py_DECREF(result);')
    lines.append('    Py_DECREF(callable);')
    lines.append('    Py_DECREF(entry_mod);')
    lines.append('    Py_Finalize();')
    lines.append('    return 0;')
    lines.append('')
    lines.append('fail:')
    lines.append('    PyConfig_Clear(&config);')
    lines.append('    Py_ExitStatusException(status);')
    lines.append('    return 1;')
    lines.append('}')

    c_source = "\n".join(lines) + "\n"
    output.write_text(c_source)
    return output


def _emit_exe_dir_helper(lines: list[str]) -> None:
    """emit a C function that resolves the directory containing the executable."""
    lines.append('/* resolve the directory containing this executable at runtime */')
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
    lines.append('    if (dir != buf) {')
    lines.append('        memmove(buf, dir, strlen(dir) + 1);')
    lines.append('    }')
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
    """emit C code that configures python's module search paths for standalone mode."""
    lines.append('    /* configure standalone python paths */')
    lines.append('    if (have_exe_dir) {')
    lines.append(f'        char home_path[PATH_MAX];')
    lines.append(f'        snprintf(home_path, sizeof(home_path), "%s/{bundle_name}", exe_dir);')
    lines.append('')
    lines.append(f'        wchar_t whome[PATH_MAX];')
    lines.append(f'        mbstowcs(whome, home_path, PATH_MAX);')
    lines.append(f'        status = PyConfig_SetString(&config, &config.home, whome);')
    lines.append(f'        if (PyStatus_Exception(status)) goto fail;')
    lines.append('')
    lines.append(f'        config.module_search_paths_set = 1;')
    lines.append(f'        char path_buf[PATH_MAX];')
    lines.append(f'        wchar_t wpath[PATH_MAX];')
    lines.append('')
    # zipped stdlib
    lines.append(f'        snprintf(path_buf, sizeof(path_buf), "%s/{bundle_name}/python{pyver_nodot}.zip", exe_dir);')
    lines.append(f'        mbstowcs(wpath, path_buf, PATH_MAX);')
    lines.append(f'        status = PyWideStringList_Append(&config.module_search_paths, wpath);')
    lines.append(f'        if (PyStatus_Exception(status)) goto fail;')
    lines.append('')
    # lib-dynload (stdlib C extensions)
    lines.append(f'        snprintf(path_buf, sizeof(path_buf), "%s/{bundle_name}/python{pyver}/lib-dynload", exe_dir);')
    lines.append(f'        mbstowcs(wpath, path_buf, PATH_MAX);')
    lines.append(f'        status = PyWideStringList_Append(&config.module_search_paths, wpath);')
    lines.append(f'        if (PyStatus_Exception(status)) goto fail;')

    if has_native_packages:
        lines.append('')
        # third-party C extensions
        lines.append(f'        snprintf(path_buf, sizeof(path_buf), "%s/{bundle_name}/site-packages", exe_dir);')
        lines.append(f'        mbstowcs(wpath, path_buf, PATH_MAX);')
        lines.append(f'        status = PyWideStringList_Append(&config.module_search_paths, wpath);')
        lines.append(f'        if (PyStatus_Exception(status)) goto fail;')

    lines.append('    }')
    lines.append('')


def _generate_startup_python(
    package_name: str | None = None,
    package_version: str | None = None,
) -> str:
    """generate minimal python startup code.

    sets sys.frozen and injects package metadata for
    importlib.metadata.version() support.
    """
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


def _c_ident(dotted_name: str) -> str:
    """convert a dotted module name to a valid C identifier."""
    return dotted_name.replace(".", "__").replace("-", "_")


def _parse_entry_point(entry_point: str) -> tuple[str, str]:
    """parse 'module.path:callable' into (module, callable)."""
    if ":" not in entry_point:
        return entry_point, "main"
    mod, call = entry_point.rsplit(":", 1)
    call = call.rstrip("()")
    return mod, call
