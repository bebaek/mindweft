from __future__ import annotations

import importlib.metadata

from minigent_workspace import application


def test_console_script_entry_point_loads_canonical_main() -> None:
    scripts = importlib.metadata.entry_points(group="console_scripts")
    entry_point = next(script for script in scripts if script.name == "minigent-coding-workspace")

    loaded = entry_point.load()
    assert loaded is application.main
