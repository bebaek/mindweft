"""Setuptools hook: source wheels always compile their matching console."""

import runpy
from pathlib import Path

from setuptools import setup
from setuptools.command.build_py import build_py


class BuildPyWithConsole(build_py):
    def run(self):
        super().run()
        # Editable checkouts serve source-tree assets; scripts/dev.py owns those.
        # Normal wheels always rebuild, regardless of pre-staged console files.
        if not self.editable_mode:
            root = Path(__file__).resolve().parent
            build_console = runpy.run_path(str(root / "_build_console.py"))["build_console"]
            build_console(root, Path(self.build_lib).resolve())


setup(cmdclass={"build_py": BuildPyWithConsole})
