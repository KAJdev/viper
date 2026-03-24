from __future__ import annotations
import ast
from viper.parser.ast_nodes import (
    Module, FuncDef, Param, Assign, Return, If, While, For,
    ExprStmt, Expr, IntLit, FloatLit, BoolLit, StrLit, NoneLit,
    Name, BinOp, UnaryOp, Compare, Call, Attribute, Subscript, FString,
)
from viper.types import VType, INT, FLOAT, BOOL, STR, NONE, ListType, DictType


class ParseError(Exception):
    def __init__(self, msg: str, line: int = 0, col: int = 0):
        self.line = line
        self.col = col
        super().__init__(f"line {line}: {msg}")


def parse(source: str, filename: str = "<input>") -> Module:
    tree = ast.parse(source, filename=filename)
    reader = _Reader(filename)
    return reader.read_module(tree)


class _Reader:
    def __init__(self, filename: str):
        self.filename = filename

    def read_module(self, node: ast.Module) -> Module:
        body = []
        main_call = None
        for stmt in node.body:
            # detect `if __name__ == "__main__": main()`
            if isinstance(stmt, ast.If) and self._is_name_main_check(stmt):
                main_call = self._extract_main_call(stmt)
                continue
            body.append(self.read_stmt(stmt))
        return Module(body=body, main_call=main_call)

    def _is_name_main_check(self, node: ast.If) -> bool:
        t = node.test
        if not isinstance(t, ast.Compare):
            return False
        if len(t.ops) != 1 or not isinstance(t.ops[0], ast.Eq):
            return False
        if not isinstance(t.left, ast.Name) or t.left.id != "__name__":
            return False
        if len(t.comparators) != 1:
            return False
        c = t.comparators[0]
        return isinstance(c, ast.Constant) and c.value == "__main__"

    def _extract_main_call(self, node: ast.If) -> Call | None:
        if len(node.body) != 1:
            return None
        stmt = node.body[0]
        if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call):
            return self.read_expr(stmt.value)  # type: ignore
        return None

    def read_stmt(self, node: ast.stmt):
        match node:
            case ast.FunctionDef():
                return self._read_funcdef(node)
            case ast.Assign():
                return self._read_assign(node)
            case ast.AnnAssign():
                return self._read_ann_assign(node)
            case ast.Return():
                return Return(
                    value=self.read_expr(node.value) if node.value else None,
                    line=node.lineno, col=node.col_offset,
                )
            case ast.If():
                return If(
                    test=self.read_expr(node.test),
                    body=[self.read_stmt(s) for s in node.body],
                    orelse=[self.read_stmt(s) for s in node.orelse],
                    line=node.lineno, col=node.col_offset,
                )
            case ast.While():
                return While(
                    test=self.read_expr(node.test),
                    body=[self.read_stmt(s) for s in node.body],
                    line=node.lineno, col=node.col_offset,
                )
            case ast.For():
                if not isinstance(node.target, ast.Name):
                    raise ParseError("only simple for-loop targets supported",
                                     node.lineno, node.col_offset)
                return For(
                    target=node.target.id,
                    iter=self.read_expr(node.iter),
                    body=[self.read_stmt(s) for s in node.body],
                    line=node.lineno, col=node.col_offset,
                )
            case ast.Expr():
                return ExprStmt(
                    value=self.read_expr(node.value),
                    line=node.lineno, col=node.col_offset,
                )
            case _:
                raise ParseError(
                    f"unsupported statement: {type(node).__name__}",
                    node.lineno, node.col_offset,
                )

    def _read_funcdef(self, node: ast.FunctionDef) -> FuncDef:
        params = []
        for arg in node.args.args:
            ann = self._read_type_annotation(arg.annotation) if arg.annotation else None
            params.append(Param(name=arg.arg, annotation=ann,
                                line=arg.lineno, col=arg.col_offset))
        ret_type = self._read_type_annotation(node.returns) if node.returns else None
        body = [self.read_stmt(s) for s in node.body]
        return FuncDef(
            name=node.name, params=params, ret_type=ret_type, body=body,
            line=node.lineno, col=node.col_offset,
        )

    def _read_assign(self, node: ast.Assign) -> Assign:
        if len(node.targets) != 1 or not isinstance(node.targets[0], ast.Name):
            raise ParseError("only simple assignment targets supported",
                             node.lineno, node.col_offset)
        return Assign(
            target=node.targets[0].id,
            value=self.read_expr(node.value),
            line=node.lineno, col=node.col_offset,
        )

    def _read_ann_assign(self, node: ast.AnnAssign) -> Assign:
        if not isinstance(node.target, ast.Name):
            raise ParseError("only simple annotated assignment targets supported",
                             node.lineno, node.col_offset)
        ann = self._read_type_annotation(node.annotation)
        return Assign(
            target=node.target.id,
            annotation=ann,
            value=self.read_expr(node.value) if node.value else NoneLit(),
            line=node.lineno, col=node.col_offset,
        )

    def read_expr(self, node: ast.expr) -> Expr:
        match node:
            case ast.Constant():
                return self._read_constant(node)
            case ast.Name():
                return Name(id=node.id, line=node.lineno, col=node.col_offset)
            case ast.BinOp():
                return BinOp(
                    left=self.read_expr(node.left),
                    op=self._binop_str(node.op),
                    right=self.read_expr(node.right),
                    line=node.lineno, col=node.col_offset,
                )
            case ast.UnaryOp():
                return UnaryOp(
                    op=self._unaryop_str(node.op),
                    operand=self.read_expr(node.operand),
                    line=node.lineno, col=node.col_offset,
                )
            case ast.Compare():
                return Compare(
                    left=self.read_expr(node.left),
                    ops=[self._cmpop_str(op) for op in node.ops],
                    comparators=[self.read_expr(c) for c in node.comparators],
                    line=node.lineno, col=node.col_offset,
                )
            case ast.Call():
                return Call(
                    func=self.read_expr(node.func),
                    args=[self.read_expr(a) for a in node.args],
                    line=node.lineno, col=node.col_offset,
                )
            case ast.Attribute():
                return Attribute(
                    value=self.read_expr(node.value),
                    attr=node.attr,
                    line=node.lineno, col=node.col_offset,
                )
            case ast.Subscript():
                return Subscript(
                    value=self.read_expr(node.value),
                    index=self.read_expr(node.slice),
                    line=node.lineno, col=node.col_offset,
                )
            case ast.JoinedStr():
                parts = []
                for v in node.values:
                    if isinstance(v, ast.Constant) and isinstance(v.value, str):
                        parts.append(StrLit(value=v.value, line=v.lineno, col=v.col_offset))
                    elif isinstance(v, ast.FormattedValue):
                        parts.append(self.read_expr(v.value))
                    else:
                        parts.append(self.read_expr(v))
                return FString(parts=parts, line=node.lineno, col=node.col_offset)
            case ast.BoolOp():
                # desugar `a and b and c` into nested BinOp
                op_str = "and" if isinstance(node.op, ast.And) else "or"
                result = self.read_expr(node.values[0])
                for v in node.values[1:]:
                    result = BinOp(left=result, op=op_str, right=self.read_expr(v),
                                   line=node.lineno, col=node.col_offset)
                return result
            case _:
                raise ParseError(
                    f"unsupported expression: {type(node).__name__}",
                    node.lineno, node.col_offset,
                )

    def _read_constant(self, node: ast.Constant) -> Expr:
        v = node.value
        ln, col = node.lineno, node.col_offset
        if isinstance(v, bool):
            return BoolLit(value=v, line=ln, col=col)
        if isinstance(v, int):
            return IntLit(value=v, line=ln, col=col)
        if isinstance(v, float):
            return FloatLit(value=v, line=ln, col=col)
        if isinstance(v, str):
            return StrLit(value=v, line=ln, col=col)
        if v is None:
            return NoneLit(line=ln, col=col)
        raise ParseError(f"unsupported constant type: {type(v).__name__}", ln, col)

    def _read_type_annotation(self, node: ast.expr) -> VType:
        match node:
            case ast.Constant(value=None):
                return NONE
            case ast.Name(id="int"):
                return INT
            case ast.Name(id="float"):
                return FLOAT
            case ast.Name(id="bool"):
                return BOOL
            case ast.Name(id="str"):
                return STR
            case ast.Name(id="None"):
                return NONE
            case ast.Subscript():
                return self._read_generic_type(node)
            case _:
                raise ParseError(
                    f"unsupported type annotation: {ast.dump(node)}",
                    node.lineno, node.col_offset,
                )

    def _read_generic_type(self, node: ast.Subscript) -> VType:
        if not isinstance(node.value, ast.Name):
            raise ParseError("unsupported generic type", node.lineno, node.col_offset)
        name = node.value.id
        if name == "list":
            elem = self._read_type_annotation(node.slice)
            return ListType(elem=elem)
        if name == "dict":
            if not isinstance(node.slice, ast.Tuple) or len(node.slice.elts) != 2:
                raise ParseError("dict requires two type args", node.lineno, node.col_offset)
            k = self._read_type_annotation(node.slice.elts[0])
            v = self._read_type_annotation(node.slice.elts[1])
            return DictType(key=k, value=v)
        raise ParseError(f"unsupported generic: {name}", node.lineno, node.col_offset)

    def _binop_str(self, op: ast.operator) -> str:
        ops = {
            ast.Add: "+", ast.Sub: "-", ast.Mult: "*", ast.Div: "/",
            ast.FloorDiv: "//", ast.Mod: "%", ast.Pow: "**",
            ast.BitAnd: "&", ast.BitOr: "|", ast.BitXor: "^",
            ast.LShift: "<<", ast.RShift: ">>",
        }
        return ops.get(type(op), "?")

    def _unaryop_str(self, op: ast.unaryop) -> str:
        ops = {ast.USub: "-", ast.UAdd: "+", ast.Not: "not", ast.Invert: "~"}
        return ops.get(type(op), "?")

    def _cmpop_str(self, op: ast.cmpop) -> str:
        ops = {
            ast.Eq: "==", ast.NotEq: "!=", ast.Lt: "<", ast.LtE: "<=",
            ast.Gt: ">", ast.GtE: ">=", ast.Is: "is", ast.IsNot: "is not",
            ast.In: "in", ast.NotIn: "not in",
        }
        return ops.get(type(op), "?")
