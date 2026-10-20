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

import json
from contextlib import suppress
from functools import partial
from typing import TYPE_CHECKING

from griffe import Alias, AliasResolutionError, Function, Kind, Parameter, ParameterKind, Parameters

if TYPE_CHECKING:
    from collections.abc import Sequence

    from griffe import Attribute, Class
    from pydantic import BaseModel

_self_namespace = "griffe_pydantic"
_mkdocstrings_namespace = "mkdocstrings"

_field_constraints = {
    "gt",
    "ge",
    "lt",
    "le",
    "multiple_of",
    "min_length",
    "max_length",
    "pattern",
    "allow_inf_nan",
    "max_digits",
    "decimal_place",
}


def _model_fields(cls: Class) -> dict[str, Attribute]:
    # Checking the kind also skips unresolved aliases to Pydantic's internal hooks.
    return {
        name: attr
        for name, attr in cls.all_members.items()
        if attr.kind is Kind.ATTRIBUTE and "pydantic-field" in attr.labels
    }  # ty: ignore[invalid-return-type]


def _model_validators(cls: Class) -> dict[str, Function]:
    return {
        name: func
        for name, func in cls.all_members.items()
        if func.kind is Kind.FUNCTION and "pydantic-validator" in func.labels
    }  # ty: ignore[invalid-return-type]


def _json_schema(model: type[BaseModel]) -> str:
    """Produce a model schema as JSON.

    Parameters:
        model: A Pydantic model.

    Returns:
        A schema as JSON.
    """
    return json.dumps(model.model_json_schema(), indent=2)


def _process_class(cls: Class) -> None:
    """Set metadata on a Pydantic model.

    Parameters:
        cls: The Griffe class representing the Pydantic model.
    """
    cls.labels.add("pydantic-model")
    cls.extra[_self_namespace]["fields"] = partial(_model_fields, cls)
    cls.extra[_self_namespace]["validators"] = partial(_model_validators, cls)
    cls.extra[_mkdocstrings_namespace]["template"] = "pydantic_model.html.jinja"


def _set_model_signature(cls: Class) -> None:
    """Expose model fields to constructor-dependent docstring parsers."""
    fields = _model_fields(cls)
    init = cls.members.get("__init__")
    if not isinstance(init, Function):
        with suppress(KeyError, AliasResolutionError):
            inherited = cls.all_members.get("__init__")
            if isinstance(inherited, Alias):
                inherited = inherited.final_target
            if isinstance(inherited, Function) and (
                inherited.lineno or "init_parameter_names" in inherited.extra[_self_namespace]
            ):
                init = inherited

    original_names = None
    if isinstance(init, Function):
        # Keep the source signature so subclasses can apply their own field defaults.
        original_names = init.extra[_self_namespace].setdefault(
            "init_parameter_names",
            tuple(param.name for param in init.parameters),
        )
        parameters = [param for param in init.parameters if param.name in original_names]
        # An explicit constructor without **kwargs cannot accept additional model fields.
        if not any(param.kind is ParameterKind.var_keyword for param in parameters):
            return
    else:
        self_name = "self"
        while self_name in fields:
            self_name = f"_{self_name}"
        parameters = [Parameter(self_name, kind=ParameterKind.positional_or_keyword)]

    names = {param.name for param in parameters}
    variadic = [param for param in parameters if param.kind is ParameterKind.var_keyword]
    parameters = [param for param in parameters if param.kind is not ParameterKind.var_keyword]
    parameters.extend(
        Parameter(
            name,
            annotation=field.annotation,
            default=field.value,
            kind=ParameterKind.keyword_only,
            docstring=field.docstring,
        )
        for name, field in fields.items()
        if name not in names
    )
    if not variadic and cls.extra[_self_namespace].get("config", {}).get("extra") == "allow":
        name = "kwargs"
        while name in fields or name in names:
            name += "_"
        variadic.append(Parameter(name, kind=ParameterKind.var_keyword))
    parameters.extend(variadic)

    if not isinstance(init, Function) or init.parent is not cls:
        init = Function("__init__", lineno=0, endlineno=0, returns="None")
        cls.set_member("__init__", init)
    if original_names is not None:
        init.extra[_self_namespace]["init_parameter_names"] = original_names
    init.parameters = Parameters(*parameters)


def _process_function(func: Function, cls: Class, fields: Sequence[str]) -> None:
    """Set metadata on a Pydantic validator.

    Parameters:
        cls: A Griffe function representing the Pydantic validator.
    """
    func.labels = {"pydantic-validator"}
    if fields and fields[0] == "*":
        targets = [member for member in cls.all_members.values() if "pydantic-field" in member.labels]
    else:
        targets = [cls.all_members[field] for field in fields]

    func.extra[_self_namespace].setdefault("targets", [])
    func.extra[_self_namespace]["targets"].extend(targets)
    for target in targets:
        target.extra[_self_namespace].setdefault("validators", [])
        target.extra[_self_namespace]["validators"].append(func)
