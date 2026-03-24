from __future__ import annotations
from viper.parser.ast_nodes import (
    Module, FuncDef, Param, Assign, Return, If, While, For,
    ExprStmt, Expr, IntLit, FloatLit, BoolLit, StrLit, NoneLit,
    Name, BinOp, UnaryOp, Compare, Call, FString, Stmt,
)
from viper.types import VType, INT, FLOAT, BOOL, STR, NONE, FuncType


class TypeError_(Exception):
    def __init__(self, msg: str, line: int = 0, col: int = 0):
        self.line = line
        self.col = col
        super().__init__(f"line {line}: {msg}")


# built-in function signatures
BUILTINS: dict[str, FuncType] = {
    "print": FuncType(params=(), ret=NONE),  # special-cased: accepts any single arg
}


class InferenceEngine:
    def __init__(self):
        # map from function name to its resolved type
        self.functions: dict[str, FuncType] = dict(BUILTINS)
        # map from variable name to type, per scope
        self.scopes: list[dict[str, VType]] = [{}]

    def infer_module(self, module: Module) -> None:
        # first pass: register all function signatures
        for stmt in module.body:
            if isinstance(stmt, FuncDef):
                self._register_func(stmt)

        # second pass: infer types within function bodies
        for stmt in module.body:
            if isinstance(stmt, FuncDef):
                self._infer_func(stmt)

        # type-check the main call if present
        if module.main_call:
            self._infer_expr(module.main_call)

    def _register_func(self, func: FuncDef) -> None:
        param_types = []
        for p in func.params:
            if p.annotation is None:
                raise TypeError_(f"parameter '{p.name}' in function '{func.name}' "
                                 f"needs a type annotation", func.line, func.col)
            param_types.append(p.annotation)
        if func.ret_type is None:
            raise TypeError_(f"function '{func.name}' needs a return type annotation",
                             func.line, func.col)
        ft = FuncType(params=tuple(param_types), ret=func.ret_type)
        self.functions[func.name] = ft

    def _push_scope(self) -> None:
        self.scopes.append({})

    def _pop_scope(self) -> None:
        self.scopes.pop()

    def _set_var(self, name: str, typ: VType) -> None:
        self.scopes[-1][name] = typ

    def _get_var(self, name: str) -> VType | None:
        for scope in reversed(self.scopes):
            if name in scope:
                return scope[name]
        return None

    def _infer_func(self, func: FuncDef) -> None:
        self._push_scope()
        for p in func.params:
            assert p.annotation is not None
            self._set_var(p.name, p.annotation)
        for stmt in func.body:
            self._infer_stmt(stmt)
        self._pop_scope()

    def _infer_stmt(self, stmt: Stmt) -> None:
        match stmt:
            case Assign():
                typ = self._infer_expr(stmt.value)
                if stmt.annotation is not None:
                    if typ != stmt.annotation and typ is not NONE:
                        raise TypeError_(
                            f"assigned type {typ} does not match annotation {stmt.annotation}",
                            stmt.line, stmt.col,
                        )
                    typ = stmt.annotation
                self._set_var(stmt.target, typ)
            case Return():
                if stmt.value:
                    self._infer_expr(stmt.value)
            case If():
                self._infer_expr(stmt.test)
                for s in stmt.body:
                    self._infer_stmt(s)
                for s in stmt.orelse:
                    self._infer_stmt(s)
            case While():
                self._infer_expr(stmt.test)
                for s in stmt.body:
                    self._infer_stmt(s)
            case For():
                iter_type = self._infer_expr(stmt.iter)
                # for now, range() yields int
                self._set_var(stmt.target, INT)
                for s in stmt.body:
                    self._infer_stmt(s)
            case ExprStmt():
                self._infer_expr(stmt.value)
            case _:
                raise TypeError_(f"unhandled statement type: {type(stmt).__name__}",
                                 stmt.line, stmt.col)

    def _infer_expr(self, expr: Expr) -> VType:
        typ = self._infer_expr_inner(expr)
        expr.type = typ
        return typ

    def _infer_expr_inner(self, expr: Expr) -> VType:
        match expr:
            case IntLit():
                return INT
            case FloatLit():
                return FLOAT
            case BoolLit():
                return BOOL
            case StrLit():
                return STR
            case NoneLit():
                return NONE
            case Name():
                t = self._get_var(expr.id)
                if t is not None:
                    return t
                if expr.id in self.functions:
                    return self.functions[expr.id]
                raise TypeError_(f"undefined name '{expr.id}'", expr.line, expr.col)
            case BinOp():
                lt = self._infer_expr(expr.left)
                rt = self._infer_expr(expr.right)
                return self._infer_binop(lt, expr.op, rt, expr.line, expr.col)
            case UnaryOp():
                ot = self._infer_expr(expr.operand)
                return self._infer_unaryop(expr.op, ot, expr.line, expr.col)
            case Compare():
                self._infer_expr(expr.left)
                for c in expr.comparators:
                    self._infer_expr(c)
                return BOOL
            case Call():
                return self._infer_call(expr)
            case FString():
                for part in expr.parts:
                    self._infer_expr(part)
                return STR
            case _:
                raise TypeError_(f"unhandled expression: {type(expr).__name__}",
                                 expr.line, expr.col)

    def _infer_binop(self, lt: VType, op: str, rt: VType, line: int, col: int) -> VType:
        if op == "+":
            if lt == STR and rt == STR:
                return STR
            if lt == INT and rt == INT:
                return INT
            if lt == FLOAT or rt == FLOAT:
                return FLOAT
        if op in ("-", "*", "//", "%"):
            if lt == INT and rt == INT:
                return INT
            if lt == FLOAT or rt == FLOAT:
                return FLOAT
        if op == "/":
            return FLOAT
        if op == "**":
            if lt == INT and rt == INT:
                return INT
            return FLOAT
        if op in ("&", "|", "^", "<<", ">>"):
            return INT
        if op in ("and", "or"):
            if lt == BOOL and rt == BOOL:
                return BOOL
            return lt
        raise TypeError_(f"unsupported operation: {lt} {op} {rt}", line, col)

    def _infer_unaryop(self, op: str, ot: VType, line: int, col: int) -> VType:
        if op == "-" or op == "+":
            if ot == INT:
                return INT
            if ot == FLOAT:
                return FLOAT
        if op == "not":
            return BOOL
        if op == "~":
            return INT
        raise TypeError_(f"unsupported unary operation: {op} {ot}", line, col)

    def _infer_call(self, call: Call) -> VType:
        if isinstance(call.func, Name):
            name = call.func.id

            # special case: print accepts any single arg
            if name == "print":
                if len(call.args) != 1:
                    raise TypeError_(f"print() takes exactly 1 argument "
                                     f"(got {len(call.args)})", call.line, call.col)
                self._infer_expr(call.args[0])
                return NONE

            # special case: range() returns an iterator of int
            if name == "range":
                for a in call.args:
                    self._infer_expr(a)
                return INT  # placeholder, for-loop handles this

            # special case: int(), float(), str(), bool() conversions
            if name in ("int", "float", "str", "bool"):
                for a in call.args:
                    self._infer_expr(a)
                return {"int": INT, "float": FLOAT, "str": STR, "bool": BOOL}[name]

            if name in self.functions:
                ft = self.functions[name]
                if len(call.args) != len(ft.params):
                    raise TypeError_(
                        f"function '{name}' expects {len(ft.params)} args, "
                        f"got {len(call.args)}", call.line, call.col,
                    )
                for arg, expected in zip(call.args, ft.params):
                    actual = self._infer_expr(arg)
                    if actual != expected:
                        raise TypeError_(
                            f"argument type mismatch: expected {expected}, got {actual}",
                            call.line, call.col,
                        )
                return ft.ret

            raise TypeError_(f"undefined function '{name}'", call.line, call.col)

        raise TypeError_(f"unsupported call target", call.line, call.col)
