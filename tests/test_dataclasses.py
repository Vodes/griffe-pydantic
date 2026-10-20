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

import logging

import pytest
from griffe import (
    Extensions,
    ParameterKind,
    Parser,
    temporary_inspected_package,
    temporary_visited_package,
)

from griffe_pydantic._internal.extension import PydanticExtension


@pytest.mark.parametrize("analysis", ["static", "dynamic"])
def test_dataclass_docstring_parameters(analysis: str, caplog: pytest.LogCaptureFixture) -> None:
    """Dataclass constructor types and defaults reach Sphinx parameter documentation."""
    code = '''
    from pydantic.dataclasses import dataclass

    @dataclass
    class Encoder:
        """An encoder.

        :param codec: Codec name.
        :param bitrate: Target bitrate.
        """

        codec: str
        bitrate: int = 256
    '''
    loader = {"static": temporary_visited_package, "dynamic": temporary_inspected_package}[analysis]
    with loader(
        "dataclass_parameters",
        modules={"__init__.py": code},
        extensions=Extensions(PydanticExtension()),
        docstring_parser=Parser.sphinx,
        search_sys_path=analysis == "dynamic",
    ) as package:
        cls = package["Encoder"]
        assert str(cls.parameters["codec"].annotation) == "str"
        assert cls.parameters["codec"].default is None
        assert str(cls.parameters["bitrate"].annotation) == "int"
        assert cls.parameters["bitrate"].default == "256"
        with caplog.at_level(logging.WARNING, logger="griffe"):
            parameters = next(section.value for section in cls.docstring.parsed if section.kind.value == "parameters")
        assert [(param.name, str(param.annotation), param.value) for param in parameters] == [
            ("codec", "str", None),
            ("bitrate", "int", "256"),
        ]
        assert not caplog.records
        assert "pydantic-model" not in cls.labels


@pytest.mark.parametrize("analysis", ["static", "dynamic"])
def test_inherited_dataclass_fields(analysis: str) -> None:
    """Encoder-style declarations retain inheritance, field options, and reexports."""
    helpers = """
    from pydantic.dataclasses import dataclass
    """
    code = """
    from dataclasses import InitVar, dataclass, field
    from typing import ClassVar
    from pydantic import Field
    from .helpers import dataclass as validated

    class CLIKwargs:
        custom_args: dict[str, str] | None = None

    @dataclass
    class Parent(CLIKwargs):
        bitrate: int = 128
        compression: int = 3
        token: InitVar[int] = 0
        codec: ClassVar[str] = "aac"
        values: list[int] = field(default_factory=list, kw_only=True)
        internal: int = field(default=0, init=False)

    @validated(init=False)
    class Encoder(Parent):
        bitrate: int = 256
        compression: int
        title: str = Field("aac", alias="name")
        output: str | None = field(default=None, kw_only=True)
    """
    loader = {"static": temporary_visited_package, "dynamic": temporary_inspected_package}[analysis]
    with loader(
        "dataclass_inheritance",
        modules={"__init__.py": code, "helpers.py": helpers},
        extensions=Extensions(PydanticExtension()),
        search_sys_path=analysis == "dynamic",
    ) as package:
        parameters = package["Encoder"].parameters
        assert [param.name for param in parameters] == [
            "self",
            "bitrate",
            "compression",
            "token",
            "name",
            "values",
            "output",
        ]
        assert parameters["bitrate"].default == "256"
        assert parameters["compression"].default == "3"
        assert str(parameters["token"].annotation) == "int"
        assert parameters["name"].default == "'aac'"
        assert parameters["values"].default is not None
        assert parameters["values"].kind is ParameterKind.keyword_only
        assert parameters["output"].kind is ParameterKind.keyword_only
        assert parameters["output"].default == "None"


@pytest.mark.parametrize("analysis", ["static", "dynamic"])
@pytest.mark.parametrize("extra", ["allow", "ignore", "forbid"])
def test_dataclass_extra_kwargs(analysis: str, extra: str) -> None:
    """Only dataclasses configured to accept extra arguments advertise them."""
    code = """
    from pydantic.dataclasses import dataclass
    from .helpers import config

    @dataclass(config=config)
    class Encoder:
        bitrate: int = 256
    """
    loader = {"static": temporary_visited_package, "dynamic": temporary_inspected_package}[analysis]
    with loader(
        f"dataclass_extras_{extra}",
        modules={
            "__init__.py": code,
            "helpers.py": f"from pydantic import ConfigDict\nconfig = ConfigDict(extra={extra!r})",
        },
        extensions=Extensions(PydanticExtension()),
        search_sys_path=analysis == "dynamic",
    ) as package:
        variadic = [param for param in package["Encoder"].parameters if param.kind is ParameterKind.var_keyword]
        assert [param.name for param in variadic] == (["kwargs"] if extra == "allow" else [])
