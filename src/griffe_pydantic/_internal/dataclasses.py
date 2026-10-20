# SPDX-License-Identifier: ISC
#
# ISC License
#
# Copyright (c) 2023, Timothée Mazzucotelli and contributors
#
# Permission to use, copy, modify, and/or distribute this software for any
# purpose with or without fee is hereby granted, provided that the above
# copyright notice and this permission notice appear in all copies.
#
# THE SOFTWARE IS PROVIDED "AS IS" AND THE AUTHOR DISCLAIMS ALL WARRANTIES
# WITH REGARD TO THIS SOFTWARE INCLUDING ALL IMPLIED WARRANTIES OF
# MERCHANTABILITY AND FITNESS. IN NO EVENT SHALL THE AUTHOR BE LIABLE FOR
# ANY SPECIAL, DIRECT, INDIRECT, OR CONSEQUENTIAL DAMAGES OR ANY DAMAGES
# WHATSOEVER RESULTING FROM LOSS OF USE, DATA OR PROFITS, WHETHER IN AN
# ACTION OF CONTRACT, NEGLIGENCE OR OTHER TORTIOUS ACTION, ARISING OUT OF
# OR IN CONNECTION WITH THE USE OR PERFORMANCE OF THIS SOFTWARE.

from __future__ import annotations

import ast
import inspect
from contextlib import suppress
from typing import TYPE_CHECKING, Any

from griffe import (
    Alias,
    AliasResolutionError,
    Attribute,
    Class,
    Expr,
    ExprCall,
    ExprDict,
    ExprKeyword,
    ExprName,
    ExprSubscript,
    ExprTuple,
    Extensions,
    Function,
    GriffeLoader,
    LoadingError,
    Module,
    Parameter,
    ParameterKind,
    Parameters,
    safe_get_annotation,
)

if TYPE_CHECKING:
    from griffe import Object


_DATACLASS = "pydantic.dataclasses.dataclass"
_FIELD_PATHS = {"dataclasses.field", "pydantic.Field", "pydantic.fields.Field", "pydantic.dataclasses.Field"}


class _Resolver:
    """Resolve source references without importing documented packages."""

    def __init__(self, loader: GriffeLoader, declarations: dict[str, Attribute | None]) -> None:
        self.loader = GriffeLoader(
            extensions=Extensions(),
            search_paths=loader.finder.search_paths,
            modules_collection=loader.modules_collection,
            lines_collection=loader.lines_collection,
            allow_inspection=False,
        )
        self.attempted: set[str] = set()
        self.declarations = declarations

    def member(self, path: str) -> Object | Alias | None:
        if path.startswith(("pydantic.", "dataclasses.", "typing.", "typing_extensions.", "builtins.", "abc.")):
            return None
        with suppress(KeyError, AliasResolutionError):
            return self.loader.modules_collection.get_member(path)
        package = path.split(".", 1)[0]
        if "." in path and package not in self.attempted and package not in self.loader.modules_collection:
            self.attempted.add(package)
            with suppress(ImportError, LoadingError):
                self.loader.load(package)
            with suppress(KeyError, AliasResolutionError):
                return self.loader.modules_collection.get_member(path)
        return None

    def path(self, expr: Expr) -> str:
        path = expr.canonical_path
        seen = set()
        while path not in seen:
            seen.add(path)
            member = self.member(path)
            if not isinstance(member, Alias):
                break
            path = member.target_path
        return path

    def value(self, value: str | Expr | None) -> str | Expr | None:
        seen = set()
        while isinstance(value, Expr) and not isinstance(value, (ExprCall, ExprDict)):
            path = self.path(value)
            if path in seen:
                break
            seen.add(path)
            member = self.member(path)
            if not isinstance(member, Attribute):
                break
            value = member.value
        return value

    def arguments(self, value: str | Expr | None) -> dict[str, str | Expr]:
        value = self.value(value)
        if isinstance(value, ExprCall):
            return {arg.name: arg.value for arg in value.arguments if isinstance(arg, ExprKeyword)}
        if isinstance(value, ExprDict):
            return {
                key: val
                for expr, val in zip(value.keys, value.values, strict=False)
                if isinstance(key := _literal(expr), str)
            }
        return {}


def _decorator(cls: Class, resolver: _Resolver, *, standard: bool = False) -> Expr | None:
    paths = {_DATACLASS, "dataclasses.dataclass"} if standard else {_DATACLASS}
    for decorator in cls.decorators:
        if isinstance(decorator.value, Expr) and resolver.path(decorator.value) in paths:
            return decorator.value
    return None


def _literal(value: str | Expr | None) -> Any:
    with suppress(ValueError, SyntaxError):
        return ast.literal_eval(str(value))
    return None


def _configuration(value: str | Expr | None, resolver: _Resolver) -> dict[str, str | Expr]:
    value = resolver.value(value)
    if isinstance(value, ExprDict) or (
        isinstance(value, ExprCall)
        and resolver.path(value)
        in {
            "dict",
            "builtins.dict",
            "pydantic.ConfigDict",
            "pydantic.config.ConfigDict",
            "pydantic.dataclasses.ConfigDict",
        }
    ):
        return resolver.arguments(value)
    return {}


def _config(cls: Class, resolver: _Resolver) -> dict[str, str | Expr]:
    for parent in [cls, *cls.mro()]:
        arguments = resolver.arguments(_decorator(parent, resolver))
        if "config" in arguments and str(arguments["config"]) != "None":
            return _configuration(arguments["config"], resolver)
        if isinstance(member := parent.members.get("__pydantic_config__"), Attribute):
            return _configuration(member.value, resolver)
    return {}


def _field_arguments(call: ExprCall, resolver: _Resolver) -> dict[str, str | Expr]:
    arguments = resolver.arguments(call)
    if call.arguments and not isinstance(call.arguments[0], ExprKeyword):
        arguments["default"] = call.arguments[0]
    return arguments


def _inherited_default(cls: Class, name: str, resolver: _Resolver) -> str | Expr | None:
    for parent in cls.mro():
        if not isinstance(member := parent.members.get(name), Attribute):
            continue
        default = member.value
        if isinstance(default, ExprCall) and resolver.path(default) in _FIELD_PATHS:
            default = _field_arguments(default, resolver).get("default")
        if default is not None:
            return default
    return None


def _parameters(cls: Class, resolver: _Resolver, config: dict[str, str | Expr]) -> dict[str, Parameter | None]:
    parameters: dict[str, Parameter | None] = {}
    kw_only = _literal(resolver.arguments(_decorator(cls, resolver, standard=True)).get("kw_only")) is True
    for attribute in cls.attributes.values():
        if attribute.is_alias:
            continue
        member = resolver.declarations.get(attribute.canonical_path, attribute)
        if member is None or member.is_alias or member.annotation is None:
            continue
        name = member.name
        annotation = member.annotation
        if isinstance(annotation, Expr) and annotation.canonical_path == "dataclasses.KW_ONLY":
            kw_only = True
            continue
        if "property" in member.labels or (
            "class-attribute" in member.labels and "instance-attribute" not in member.labels
        ):
            parameters[name] = None
            continue

        arguments = {}
        if (
            isinstance(annotation, ExprSubscript)
            and annotation.canonical_path in {"typing.Annotated", "typing_extensions.Annotated"}
            and isinstance(annotation.slice, ExprTuple)
        ):
            for metadata in annotation.slice.elements[1:]:
                if isinstance(metadata, ExprCall) and resolver.path(metadata) in _FIELD_PATHS:
                    arguments.update(_field_arguments(metadata, resolver))
            annotation = annotation.slice.elements[0]
        if isinstance(annotation, ExprSubscript) and annotation.canonical_path == "dataclasses.InitVar":
            annotation = annotation.slice

        default = member.value
        if isinstance(default, ExprCall) and resolver.path(default) in _FIELD_PATHS:
            arguments.update(_field_arguments(default, resolver))
            default = None
        elif default is None and not {"default", "default_factory"}.intersection(arguments):
            default = _inherited_default(cls, name, resolver)
        if _literal(arguments.get("init")) is False:
            parameters[name] = None
            continue
        if default is None:
            default = arguments.get("default")
        if factory := arguments.get("default_factory"):
            default = ExprCall(
                function=factory if isinstance(factory, Expr) else ExprName(factory, parent=cls),
                arguments=[],
            )
        if str(default) in {"...", "PydanticUndefined", "dataclasses.MISSING"}:
            default = None

        alias = arguments.get("validation_alias", arguments.get("alias"))
        if _literal(config.get("validate_by_alias")) is not False:
            alias_value = _literal(alias)
            if isinstance(alias_value, str) and alias_value.isidentifier():
                name = alias_value
        parameters[member.name] = Parameter(
            name,
            annotation=annotation,
            default=default,
            kind=ParameterKind.keyword_only
            if _literal(arguments.get("kw_only", str(kw_only))) is True
            else ParameterKind.positional_or_keyword,
            docstring=member.docstring,
        )
    return parameters


def _set_init(cls: Class, parameters: list[Parameter], *, extra: bool) -> None:
    names = {param.name for param in parameters}
    self_name = "self"
    while self_name in names:
        self_name = f"_{self_name}"
    if extra and not any(param.kind is ParameterKind.var_keyword for param in parameters):
        name = "kwargs"
        while name in names:
            name += "_"
        parameters.append(Parameter(name, kind=ParameterKind.var_keyword))
    cls.labels.add("dataclass")
    cls.set_member(
        "__init__",
        Function(
            "__init__",
            parameters=Parameters(Parameter(self_name, kind=ParameterKind.positional_or_keyword), *parameters),
            returns="None",
            lineno=0,
            endlineno=0,
        ),
    )


def _process_class(cls: Class, resolver: _Resolver) -> None:
    if _decorator(cls, resolver) is None:
        return
    # Load source bases before asking Griffe for its C3 inheritance order.
    pending = [cls]
    seen = set()
    while pending:
        current = pending.pop()
        if current.path in seen:
            continue
        seen.add(current.path)
        for base in current.bases:
            if isinstance(base, Expr):
                parent = resolver.member(resolver.path(base))
                if isinstance(parent, Class):
                    pending.append(parent)
    config = _config(cls, resolver)
    parameters: dict[str, Parameter | None] = {}
    for parent in [*reversed(cls.mro()), cls]:
        if _decorator(parent, resolver, standard=True) is not None:
            parameters.update(_parameters(parent, resolver, config))
    ordered = [param for param in parameters.values() if param is not None]
    ordered.sort(key=lambda param: param.kind is ParameterKind.keyword_only)
    _set_init(cls, ordered, extra=_literal(config.get("extra")) == "allow")


def _process_module(
    pkg: Module,
    *,
    declarations: dict[str, Attribute | None],
    loader: GriffeLoader | None = None,
) -> None:
    """Build Pydantic dataclass constructors from source declarations."""
    resolver = _Resolver(loader or GriffeLoader(modules_collection=pkg.modules_collection), declarations)

    def recurse(obj: Module | Class) -> None:
        if isinstance(obj, Class) and obj.analysis == "static":
            _process_class(obj, resolver)
        for member in list(obj.members.values()):
            if isinstance(member, Module | Class):
                recurse(member)

    recurse(pkg)


def _process_dynamic(obj: type, cls: Class) -> None:
    """Use the runtime class signature instead of Pydantic's generic initializer."""
    parameters = []
    for parameter in inspect.signature(obj).parameters.values():
        annotation = None
        if parameter.annotation is not inspect.Parameter.empty:
            value = inspect.formatannotation(parameter.annotation)
            with suppress(SyntaxError):
                annotation = safe_get_annotation(ast.parse(value, mode="eval").body, parent=cls)
                if isinstance(annotation, ExprSubscript) and annotation.canonical_path == "dataclasses.InitVar":
                    annotation = annotation.slice
        parameters.append(
            Parameter(
                parameter.name,
                annotation=annotation,
                default=None if parameter.default is inspect.Parameter.empty else repr(parameter.default),
                kind=ParameterKind[parameter.kind.name.lower()],
            ),
        )
    _set_init(cls, parameters, extra=getattr(obj, "__pydantic_config__", {}).get("extra") == "allow")
