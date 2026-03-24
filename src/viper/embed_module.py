"""generate a cpython extension module (.so) with embedded frozen bytecode.

when imported, the extension installs a frozen module finder into sys.meta_path,
making the embedded package available through normal import statements.
"""
from __future__ import annotations
from pathlib import Path
from viper.freezer import FrozenModule


def generate_module_c(
    modules: list[FrozenModule],
    module_name: str,
    output: Path,
    package_name: str | None = None,
    package_version: str | None = None,
) -> Path:
    """generate a C extension module that provides frozen bytecode via import hooks.

    the extension exports a single module `_<module_name>_frozen` that, on first
    import, installs a meta path finder for all embedded modules.
    """
    ext_mod_name = f"_{module_name}_frozen"
    lines: list[str] = []
    lines.append("#include <Python.h>")
    lines.append("#include <string.h>")
    lines.append("")

    # emit bytecode arrays
    for mod in modules:
        c_name = _c_ident(mod.name)
        data = mod.bytecode
        lines.append(f"static const unsigned char frozen_{c_name}[] = {{")
        for i in range(0, len(data), 16):
            chunk = data[i : i + 16]
            hex_vals = ", ".join(f"0x{b:02x}" for b in chunk)
            lines.append(f"    {hex_vals},")
        lines.append("};")
        lines.append("")

    # emit frozen table
    lines.append("typedef struct {")
    lines.append("    const char *name;")
    lines.append("    const unsigned char *data;")
    lines.append("    Py_ssize_t size;")
    lines.append("    int is_package;")
    lines.append("} viper_frozen_entry;")
    lines.append("")
    lines.append("static const viper_frozen_entry viper_frozen_table[] = {")
    for mod in modules:
        c_name = _c_ident(mod.name)
        pkg = 1 if mod.is_package else 0
        lines.append(
            f'    {{"{mod.name}", frozen_{c_name}, '
            f"sizeof(frozen_{c_name}), {pkg}}},"
        )
    lines.append("    {NULL, NULL, 0, 0}")
    lines.append("};")
    lines.append("")

    # C functions exposed to python
    lines.append(
        "static PyObject* viper_frozen_find(PyObject *self, PyObject *args) {"
    )
    lines.append("    const char *name;")
    lines.append('    if (!PyArg_ParseTuple(args, "s", &name)) return NULL;')
    lines.append(
        "    for (const viper_frozen_entry *e = viper_frozen_table; e->name; e++) {"
    )
    lines.append("        if (strcmp(e->name, name) == 0) {")
    lines.append(
        '            return Py_BuildValue("(y#i)", e->data, e->size, e->is_package);'
    )
    lines.append("        }")
    lines.append("    }")
    lines.append("    Py_RETURN_NONE;")
    lines.append("}")
    lines.append("")
    lines.append(
        "static PyObject* viper_frozen_list(PyObject *self, PyObject *args) {"
    )
    lines.append("    PyObject *lst = PyList_New(0);")
    lines.append(
        "    for (const viper_frozen_entry *e = viper_frozen_table; e->name; e++) {"
    )
    lines.append("        PyObject *name = PyUnicode_FromString(e->name);")
    lines.append("        PyList_Append(lst, name);")
    lines.append("        Py_DECREF(name);")
    lines.append("    }")
    lines.append("    return lst;")
    lines.append("}")
    lines.append("")

    # generate the importer python code as a C string
    importer_py = _generate_module_importer(ext_mod_name, package_name, package_version)
    escaped = (
        importer_py.replace("\\", "\\\\").replace('"', '\\"').replace("\n", '\\n"\n"')
    )
    lines.append(f'static const char *viper_importer_code = "{escaped}";')
    lines.append("")

    # install function: called automatically when the extension is imported
    lines.append(
        "static PyObject* viper_install(PyObject *self, PyObject *args) {"
    )
    lines.append("    if (PyRun_SimpleString(viper_importer_code) != 0) {")
    lines.append(
        '        PyErr_SetString(PyExc_RuntimeError, "failed to install viper frozen importer");'
    )
    lines.append("        return NULL;")
    lines.append("    }")
    lines.append("    Py_RETURN_NONE;")
    lines.append("}")
    lines.append("")

    # method table
    lines.append("static PyMethodDef methods[] = {")
    lines.append('    {"find", viper_frozen_find, METH_VARARGS, NULL},')
    lines.append('    {"list_modules", viper_frozen_list, METH_NOARGS, NULL},')
    lines.append('    {"install", viper_install, METH_NOARGS, NULL},')
    lines.append("    {NULL, NULL, 0, NULL}")
    lines.append("};")
    lines.append("")

    # module exec function (PEP 489 multi-phase init)
    lines.append("static int module_exec(PyObject *mod) {")
    lines.append("    if (PyRun_SimpleString(viper_importer_code) != 0) {")
    lines.append(
        '        PyErr_SetString(PyExc_RuntimeError, "failed to install viper frozen importer");'
    )
    lines.append("        return -1;")
    lines.append("    }")
    lines.append("    return 0;")
    lines.append("}")
    lines.append("")

    lines.append("static PyModuleDef_Slot module_slots[] = {")
    lines.append("    {Py_mod_exec, module_exec},")
    lines.append("    {0, NULL}")
    lines.append("};")
    lines.append("")

    # module definition
    lines.append("static struct PyModuleDef module_def = {")
    lines.append(
        f'    PyModuleDef_HEAD_INIT, "{ext_mod_name}", NULL, 0, methods, module_slots'
    )
    lines.append("};")
    lines.append("")

    # init function
    lines.append(f"PyMODINIT_FUNC PyInit_{ext_mod_name}(void) {{")
    lines.append("    return PyModuleDef_Init(&module_def);")
    lines.append("}")
    lines.append("")

    c_source = "\n".join(lines) + "\n"
    output.write_text(c_source)
    return output


def generate_module_init(
    module_name: str,
    output_dir: Path,
    package_name: str | None = None,
    package_version: str | None = None,
) -> Path:
    """generate an __init__.py that loads the frozen extension on import.

    the generated file imports the C extension, which triggers installation of
    the frozen import hook. subsequent imports of the package's submodules
    resolve through the frozen bytecode.
    """
    ext_mod_name = f"_{module_name}_frozen"
    init_path = output_dir / module_name / "__init__.py"
    init_path.parent.mkdir(parents=True, exist_ok=True)

    lines = [
        f"# auto-generated by viper -- imports frozen bytecode from {ext_mod_name}.so",
        f"import {ext_mod_name}  # installs frozen importer on load",
        f"",
        f"# re-export everything from the frozen package",
        f"from {module_name}._viper_reexport import *  # noqa: F401,F403",
    ]

    # the reexport module is provided by the frozen bytecode
    # (it's the original __init__.py, which gets loaded via the frozen importer)

    # simpler approach: just trigger the frozen importer and then re-import ourselves
    lines = [
        f"# auto-generated by viper",
        f"import {ext_mod_name} as _ext  # loads frozen bytecode + installs importer",
        f"",
        f"# the frozen importer is now active. re-export the frozen package contents.",
        f"import importlib as _il",
        f"_frozen = _il.import_module('{module_name}')",
        f"globals().update({{k: v for k, v in _frozen.__dict__.items() if not k.startswith('_')}})",
    ]

    # actually, even simpler: the extension auto-installs the importer on load,
    # but we need to avoid infinite recursion since this __init__.py IS the
    # package being imported. the frozen importer takes priority in sys.meta_path,
    # so once the extension is loaded, the frozen __init__.py bytecode runs instead.
    #
    # the trick: this __init__.py only exists in the wheel. the frozen importer
    # is inserted at position 0 in sys.meta_path. on first import of the package,
    # the normal file finder loads this __init__.py, which loads the extension,
    # which installs the frozen finder. subsequent submodule imports go through frozen.

    init_path.write_text(
        f"# auto-generated by viper\n"
        f"import {ext_mod_name}  # installs frozen import hook\n"
    )
    return init_path


def _generate_module_importer(
    ext_mod_name: str,
    package_name: str | None = None,
    package_version: str | None = None,
) -> str:
    """generate python code for the frozen module importer (module mode).

    similar to the binary importer but references the extension module by name
    instead of the built-in _viper_frozen module.
    """
    metadata_block = ""
    if package_name and package_version:
        pn_lower = package_name.replace("-", "_").lower()
        metadata_block = f"""
try:
    import importlib.metadata as _md
    import email.message

    class _ViperDist(_md.Distribution):
        def __init__(self):
            self._meta = email.message.Message()
            self._meta["Name"] = "{package_name}"
            self._meta["Version"] = "{package_version}"
        def read_text(self, filename):
            if filename == "METADATA":
                return "Name: {package_name}\\nVersion: {package_version}\\n"
            return None
        def locate_file(self, path):
            return path

    class _ViperDistFinder(_md.DistributionFinder):
        def find_distributions(self, context=_md.DistributionFinder.Context()):
            name = context.name
            if name and name.replace("-", "_").lower() in ("{pn_lower}", "{package_name.lower()}"):
                return [_ViperDist()]
            if not name:
                return [_ViperDist()]
            return []

    sys.meta_path.append(_ViperDistFinder())
except Exception:
    pass
"""

    return f"""
import sys
import marshal
import importlib
import importlib.abc
import importlib.machinery
import {ext_mod_name} as _viper_ext

class ViperFrozenFinder(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path, target=None):
        result = _viper_ext.find(fullname)
        if result is None:
            return None
        bytecode, is_package = result
        loader = ViperFrozenLoader(fullname, bytecode, is_package)
        spec = importlib.machinery.ModuleSpec(
            fullname, loader,
            origin="viper-frozen:" + fullname,
            is_package=bool(is_package),
        )
        if is_package:
            spec.submodule_search_locations = []
        return spec

class ViperFrozenLoader(importlib.abc.Loader):
    def __init__(self, name, bytecode, is_package):
        self.name = name
        self.bytecode = bytecode
        self.is_package = is_package

    def create_module(self, spec):
        return None

    def exec_module(self, module):
        code = marshal.loads(self.bytecode)
        exec(code, module.__dict__)

sys.meta_path.insert(0, ViperFrozenFinder())
{metadata_block}
"""


def _c_ident(dotted_name: str) -> str:
    """convert a dotted module name to a valid C identifier."""
    return dotted_name.replace(".", "__").replace("-", "_")
