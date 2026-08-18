from __future__ import annotations

import importlib.metadata

import pytest

from minigent_workspace import application


@pytest.mark.parametrize("script_name", ["mindweft-coding-workspace", "minigent-coding-workspace"])
def test_console_script_entry_points_load_canonical_main(script_name: str) -> None:
    scripts = importlib.metadata.entry_points(group="console_scripts")
    entry_point = next(script for script in scripts if script.name == script_name)

    loaded = entry_point.load()
    assert loaded is application.main
