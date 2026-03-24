from __future__ import annotations
from dataclasses import dataclass, field
from viper.types import VType


@dataclass
class Node:
    """base for all AST nodes"""
    line: int = 0
    col: int = 0


# -- expressions --

@dataclass
class Expr(Node):
    type: VType | None = field(default=None, repr=False)


@dataclass
class IntLit(Expr):
    value: int = 0


@dataclass
class FloatLit(Expr):
    value: float = 0.0


@dataclass
class BoolLit(Expr):
    value: bool = False


@dataclass
class StrLit(Expr):
    value: str = ""


@dataclass
class NoneLit(Expr):
    pass


@dataclass
class Name(Expr):
    id: str = ""


@dataclass
class BinOp(Expr):
    left: Expr = field(default_factory=Expr)
    op: str = ""
    right: Expr = field(default_factory=Expr)


@dataclass
class UnaryOp(Expr):
    op: str = ""
    operand: Expr = field(default_factory=Expr)


@dataclass
class Compare(Expr):
    left: Expr = field(default_factory=Expr)
    ops: list[str] = field(default_factory=list)
    comparators: list[Expr] = field(default_factory=list)


@dataclass
class Call(Expr):
    func: Expr = field(default_factory=Expr)
    args: list[Expr] = field(default_factory=list)


@dataclass
class Attribute(Expr):
    value: Expr = field(default_factory=Expr)
    attr: str = ""


@dataclass
class Subscript(Expr):
    value: Expr = field(default_factory=Expr)
    index: Expr = field(default_factory=Expr)


@dataclass
class FString(Expr):
    parts: list[Expr] = field(default_factory=list)


# -- statements --

@dataclass
class Stmt(Node):
    pass


@dataclass
class ExprStmt(Stmt):
    value: Expr = field(default_factory=Expr)


@dataclass
class Assign(Stmt):
    target: str = ""
    annotation: VType | None = None
    value: Expr = field(default_factory=Expr)


@dataclass
class Return(Stmt):
    value: Expr | None = None


@dataclass
class If(Stmt):
    test: Expr = field(default_factory=Expr)
    body: list[Stmt] = field(default_factory=list)
    orelse: list[Stmt] = field(default_factory=list)


@dataclass
class While(Stmt):
    test: Expr = field(default_factory=Expr)
    body: list[Stmt] = field(default_factory=list)


@dataclass
class For(Stmt):
    target: str = ""
    iter: Expr = field(default_factory=Expr)
    body: list[Stmt] = field(default_factory=list)


@dataclass
class FuncDef(Stmt):
    name: str = ""
    params: list[Param] = field(default_factory=list)
    ret_type: VType | None = None
    body: list[Stmt] = field(default_factory=list)


@dataclass
class Param(Node):
    name: str = ""
    annotation: VType | None = None


@dataclass
class Module(Node):
    body: list[Stmt] = field(default_factory=list)
    # the entry point expression found inside `if __name__ == "__main__":`
    main_call: Call | None = None
