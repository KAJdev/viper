from __future__ import annotations
from dataclasses import dataclass


@dataclass(frozen=True)
class VType:
    """base class for all viper types"""
    pass


@dataclass(frozen=True)
class IntType(VType):
    pass


@dataclass(frozen=True)
class FloatType(VType):
    pass


@dataclass(frozen=True)
class BoolType(VType):
    pass


@dataclass(frozen=True)
class StrType(VType):
    pass


@dataclass(frozen=True)
class NoneType(VType):
    pass


@dataclass(frozen=True)
class ListType(VType):
    elem: VType


@dataclass(frozen=True)
class DictType(VType):
    key: VType
    value: VType


@dataclass(frozen=True)
class TupleType(VType):
    elems: tuple[VType, ...]


@dataclass(frozen=True)
class FuncType(VType):
    params: tuple[VType, ...]
    ret: VType


@dataclass(frozen=True)
class TypeVar(VType):
    """unresolved type, used during inference"""
    name: str


# singletons for convenience
INT = IntType()
FLOAT = FloatType()
BOOL = BoolType()
STR = StrType()
NONE = NoneType()
