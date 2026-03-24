# viper

python to native code compiler. write regular python, compile to a static binary or an importable python module.

## goals

- accept standard python with PEP 484 type hints
- produce fast, small, standalone static binaries
- produce importable python modules (C extension or ctypes-backed .so) from the same source
- generate readable, auditable C as the intermediate representation
- no runtime dependency on cpython for the binary target

## non-goals

- supporting the full dynamic surface of python (eval, exec, metaclasses, monkey-patching)
- being a drop-in replacement for cpython
- competing with mojo/codon on GPU workloads (for now)

## architecture

```
                         ┌─────────────┐
   .py files ──────────► │   parser    │  python ast module -> viper AST
                         └──────┬──────┘
                                │
                         ┌──────▼──────┐
                         │    type     │  constraint-based inference + PEP 484 hints
                         │  inference  │
                         └──────┬──────┘
                                │
                         ┌──────▼──────┐
                         │  viper IR   │  typed, SSA-form intermediate repr (future)
                         └──────┬──────┘
                                │
                    ┌───────────┼───────────┐
                    │           │           │
             ┌──────▼──┐ ┌─────▼───┐ ┌─────▼───┐
             │  C back  │ │  llvm   │ │  rust   │
             │  -end    │ │ backend │ │ backend │
             └──────┬───┘ └────┬────┘ └────┬────┘
                    │          │           │
                    └──────────┼───────────┘
                               │
                    ┌──────────▼──────────┐
                    │      linker         │
                    │  (binary / module)  │
                    └─────────────────────┘
```

## build modes

a single source file (or package) compiles into multiple artifacts depending on flags:

| flag | output | description |
|------|--------|-------------|
| `--binary` | standalone executable | static binary, no python dependency |
| `--module` | cpython C extension (.so/.pyd) | importable via `import foo`, fastest FFI |
| `--cdll` | shared lib + ctypes stub | plain .so + generated .py loader, version-agnostic |
| `--package` | all of the above + pyproject.toml | ready for distribution |

the core generated C is identical across modes. only the entry point shim differs:
- `--binary` links `shim_main.c` (contains `main()`)
- `--module` links `shim_cpython.c` (contains `PyInit_foo()` + PyArg marshaling)
- `--cdll` links as plain `.so` and generates a `.py` ctypes wrapper

### dual-mode example

given this source:

```python
def add(x: int, y: int) -> int:
    return x + y

def main() -> None:
    print(add(1, 2))

if __name__ == "__main__":
    main()
```

`viper build example.py --binary` produces a static binary where `main()` is the entry point.

`viper build example.py --module` produces a `.so` that python can import:

```python
>>> import example
>>> example.add(1, 2)
3
```

both share the same compiled core. the binary links a `main()` wrapper. the module links a `PyInit_example()` wrapper with type marshaling.

## type strategy

### sources of type information (in priority order)

1. explicit PEP 484 annotations (`def foo(x: int) -> str:`)
2. annotated assignments (`x: int = 5`)
3. literal inference (`x = 5` -> int, `x = "hello"` -> str)
4. constraint propagation (if `x + y` and `x: int`, then `y` must be numeric)
5. return type inference from body analysis

### rules

- all function signatures at module boundaries must be fully typed (params + return)
- local variables can be inferred from assignment
- if a type cannot be resolved, emit a clear error pointing to the exact location and suggesting what annotation to add
- no implicit Any -- every value has a concrete type at compile time

### supported types (M0-M2)

| python type | C representation | notes |
|-------------|-----------------|-------|
| `int` | `int64_t` | 64-bit signed integer |
| `float` | `double` | 64-bit float |
| `bool` | `int8_t` | 0 or 1 |
| `str` | `viper_str*` | refcounted, length-prefixed, utf-8 |
| `None` | `void` | return type only |
| `list[T]` | `viper_list_T*` | refcounted, generic over element type |
| `dict[K,V]` | `viper_dict_KV*` | refcounted, generic over key/value types |
| `tuple[T,...]` | struct | fixed-size, stack-allocated |

## memory management

- reference counting for all heap-allocated objects (str, list, dict)
- refcount pinned to INT64_MAX for string literals and other statics (never freed)
- escape analysis (future): stack-allocate objects that don't escape their scope
- arena allocation (future): batch-free short-lived object graphs
- no tracing GC initially -- cycles are a known limitation, documented clearly

## C runtime (`viper_rt`)

minimal C library linked into every compiled program:

- `viper_str*` -- string type with concat, comparison, slicing, formatting
- `viper_list_T*` -- generic list with append, indexing, iteration
- `viper_dict_KV*` -- generic dict with get, set, iteration
- `viper_print_*` -- type-specialized print functions
- `viper_incref` / `viper_decref` -- reference counting
- `viper_runtime_init` / `viper_runtime_cleanup` -- lifecycle hooks

## module structure

```
viper/
├── pyproject.toml
├── src/viper/
│   ├── cli.py                  # entry point: viper build foo.py
│   ├── types.py                # type system definitions
│   ├── parser/
│   │   ├── reader.py           # python ast -> viper AST
│   │   └── ast_nodes.py        # typed AST node definitions
│   ├── typeinfer/
│   │   └── engine.py           # constraint-based type inference
│   ├── ir/
│   │   ├── nodes.py            # SSA-form IR nodes (future)
│   │   ├── builder.py          # AST -> IR lowering (future)
│   │   └── passes.py           # optimization passes (future)
│   ├── codegen/
│   │   └── c/
│   │       ├── emitter.py      # typed AST -> C source
│   │       ├── shim_binary.py  # main() wrapper generation
│   │       ├── shim_cpython.py # PyInit + marshaling generation
│   │       └── shim_ctypes.py  # .py ctypes stub generation
│   ├── linker.py               # drives cc to produce binary / .so
│   └── packager.py             # generates pyproject.toml, wheel layout
├── runtime/
│   ├── viper_rt.h              # runtime header
│   └── viper_rt.c              # runtime implementation
└── tests/
    ├── test_parser.py
    ├── test_typeinfer.py
    ├── test_codegen.py
    └── programs/               # end-to-end test programs
```

## milestones

### M0: hello world (current)

**input:**
```python
def main() -> None:
    print("hello world")

if __name__ == "__main__":
    main()
```

**what works:**
- parse python source to viper AST
- type-check with inference engine
- emit C code (core functions + main shim)
- compile to static binary via cc
- `--module` mode generating importable .so

**generated C:**
```c
#include "viper_rt.h"

void viper__example__main(void) {
    viper_print_str(viper_str_lit("hello world"));
}
```

### M1: functions + arithmetic + control flow

- function definitions with typed params and returns
- int/float/bool arithmetic and comparisons
- if/elif/else, while, for/range
- local variables with type inference
- recursive functions

### M2: strings, lists, dicts

- string operations: concat, slicing, f-strings, len(), methods
- list[T]: append, indexing, iteration, slicing, len()
- dict[K,V]: get/set, iteration, keys/values/items
- type inference for container literals

### M3: classes

- class definitions with typed fields
- methods (self parameter)
- __init__ constructor
- single inheritance
- isinstance() checks

### M4: closures + iterators

- nested functions with captured variables
- lambda expressions
- generator functions (yield)
- iterator protocol (__iter__/__next__)
- list/dict/set comprehensions

### M5: stdlib + real programs

- file I/O (open, read, write)
- os.path basics
- json encode/decode
- sys.argv
- math module
- itertools subset
- collections subset
- error handling (try/except/raise with typed exceptions)
- real-world test programs (CLI tools, data processors)

## python subset: what compiles and what doesn't

### compiles

```python
# typed functions
def fib(n: int) -> int:
    if n <= 1:
        return n
    return fib(n - 1) + fib(n - 2)

# type-inferred locals
x = 42              # inferred as int
name = "viper"      # inferred as str

# standard control flow
for i in range(10):
    if i % 2 == 0:
        print(i)

# containers with type params
scores: list[int] = [1, 2, 3]
lookup: dict[str, int] = {"a": 1, "b": 2}

# classes (M3+)
class Point:
    def __init__(self, x: float, y: float) -> None:
        self.x = x
        self.y = y

    def distance(self, other: Point) -> float:
        return ((self.x - other.x) ** 2 + (self.y - other.y) ** 2) ** 0.5

# f-strings
print(f"result: {fib(10)}")
```

### does not compile (with clear errors)

```python
x = []                  # error: cannot infer element type, use x: list[int] = []
def foo(x): ...         # error: parameter 'x' needs a type annotation
eval("1 + 2")           # error: eval() is not supported
globals()["x"] = 1      # error: globals() is not supported
```

### never supported

- `eval()`, `exec()`, `compile()`
- `globals()`, `locals()` mutation
- `__import__()` dynamic imports
- metaclasses, `__class__` manipulation
- monkey-patching (runtime attribute injection on classes)
- `*args`, `**kwargs` (M1+ may add limited support)
