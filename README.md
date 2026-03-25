# viper

compile python packages into standalone binaries and importable native extensions.

```
$ uvx viperc build ./myapp
built: ./myapp (17,307,272 bytes, bundle 85,344,541 bytes)
built: ./myapp_pkg.cpython-314-darwin.so (17,308,416 bytes)
```

one command, two outputs:

```bash
./myapp --version          # standalone binary, no python needed
python -c "import myapp"   # importable .so, same frozen bytecode
```

## how it works

viper freezes your python source as bytecode into a native executable using cpython's `PyImport_FrozenModules`. the binary bundles a cpython dylib and zipped stdlib so it runs anywhere with zero dependencies. the `.so` module uses the same bytecode blob but loads as a normal python extension.

```
myapp                               17MB executable (~1300 frozen modules)
myapp_pkg.cpython-314-darwin.so     17MB importable extension module
myapp.lib/
├── libpython3.14.dylib
├── python314.zip                   (stdlib with pre-compiled .pyc)
├── python3.14/lib-dynload/
└── site-packages/                  (C extensions, data files, dist-info)
```

pure python deps get frozen as bytecode directly in the binary. packages with C extensions or data files get bundled to `site-packages/`.

## install

```
# run directly
uvx viperc build ./mypackage

# or install
uv tool install viperc
```

requires a C compiler (`cc`). python-build-standalone is downloaded automatically on first build.

## usage

```
viperc build <path> [options]
```

| flag | description |
|------|-------------|
| `--python 3.12` | target python version (default: 3.14) |
| `-o path` | output binary path |
| `--entry-point mod:fn` | override entry point (auto-detected from pyproject.toml) |
| `--no-deps` | skip bundling third-party dependencies |
| `-v` | verbose output |

entry points are read from `[project.scripts]` in `pyproject.toml`:

```toml
[project.scripts]
myapp = "mypackage.cli:main"
```

## target python version

the `--python` flag controls which cpython version gets embedded. viper itself runs on any python 3.12+, but the output targets whatever you specify:

```bash
uvx viperc build ./app --python 3.12   # binary embeds cpython 3.12
uvx viperc build ./app --python 3.14   # binary embeds cpython 3.14
```

bytecode is cross-compiled and deps are installed for the target version automatically.

## what gets frozen vs bundled

| package type | handling |
|-------------|----------|
| pure python | frozen as bytecode in the binary |
| has `.so` files | copied to `<name>.lib/site-packages/` |
| has data files | copied to `<name>.lib/site-packages/` |
| `.dist-info` dirs | always copied (for `importlib.metadata`) |

detection is automatic -- if a package contains any file that isn't source code (`.py`, `.pyi`, `.pyx`, `.c`, `.h`), it gets bundled instead of frozen.

## limitations

- macOS (arm64, x86_64) and linux (x86_64, aarch64)
- no windows support yet
- frozen modules don't have `__file__` (some libraries check this)
- first run is slower (~2-4s) due to OS disk cache warmup
