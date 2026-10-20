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

import pytest
from griffe import Extensions, Parser, temporary_inspected_package, temporary_visited_package

from griffe_pydantic._internal.extension import PydanticExtension


@pytest.mark.parametrize("analysis", ["static", "dynamic"])
def test_model_docstring_defaults(analysis: str, caplog: pytest.LogCaptureFixture) -> None:
    """Model parameters retain defaults, including inherited fields accepted through kwargs."""
    code = '''
    from pathlib import Path
    from pydantic import BaseModel, Field

    class Settings(BaseModel):
        show_name: str = "Example"
        clean_work_dirs: bool = False

        def __init__(self, **options):
            super().__init__(**options)

    class SettingsOverride(Settings):
        show_name: str = "Overridden"

    class Setup(Settings):
        """Environment settings.

        :param episode: Episode number.
        :param config_path: Configuration path.
        :param show_name: Show name.
        :param clean_work_dirs: Remove temporary files.
        :param work_dir: Temporary directory.
        """
        episode: str = "01"
        config_path: str | None = None
        work_dir: str | None = None

        def __init__(self, episode: str = "01", config_path: str | Path | None = None, **options):
            super().__init__(episode=episode, config_path=config_path, **options)

    def make_items() -> list[int]:
        raise AssertionError("Documentation must not call default factories")

    class Metadata(BaseModel):
        """Metadata with optional values and one required identifier.

        :param identifier: Required identifier.
        :param primaries: Optional primaries.
        :param max_cll: Optional light level.
        :param items: Values supplied by a factory.
        """
        identifier: int
        primaries: tuple[float, float] | None = None
        max_cll: int | None = Field(default=None, ge=0)
        items: list[int] = Field(default_factory=make_items)
    '''
    loader = {"static": temporary_visited_package, "dynamic": temporary_inspected_package}[analysis]
    with loader(
        "model_docstring_defaults",
        modules={"__init__.py": code},
        extensions=Extensions(PydanticExtension()),
        docstring_parser=Parser.sphinx,
        search_sys_path=analysis == "dynamic",
    ) as package:
        assert package["Settings"].parameters["show_name"].default == "'Example'"
        assert package["SettingsOverride"].parameters["show_name"].default == "'Overridden'"
        setup = package["Setup"]
        setup_parameters = {
            param.name: param
            for section in setup.docstring.parsed
            if section.kind.value == "parameters"
            for param in section.value
        }
        assert setup_parameters["episode"].value == "'01'"
        assert str(setup_parameters["config_path"].annotation).replace("pathlib.", "") == "str | Path | None"
        assert setup_parameters["config_path"].value == "None"
        assert setup_parameters["show_name"].value == "'Example'"
        assert str(setup_parameters["show_name"].annotation) == "str"
        assert setup_parameters["clean_work_dirs"].value == "False"
        assert setup_parameters["work_dir"].value == "None"
        assert setup.parameters[-1].name == "options"
        assert setup.members["__init__"].source

        metadata = package["Metadata"]
        metadata_parameters = {
            param.name: param
            for section in metadata.docstring.parsed
            if section.kind.value == "parameters"
            for param in section.value
        }
        assert metadata_parameters["identifier"].value is None
        assert str(metadata_parameters["identifier"].annotation) == "int"
        assert metadata_parameters["primaries"].value == "None"
        assert metadata_parameters["max_cll"].value == "None"
        assert str(metadata_parameters["max_cll"].annotation) == "int | None"
        assert str(metadata_parameters["items"].value) == "make_items()"
        assert not caplog.records
