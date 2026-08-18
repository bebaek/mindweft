"""Legacy compatibility alias for :mod:`mindweft_workspace.mcp_resolution`."""

from importlib import import_module as _import_module
from sys import modules as _modules

from mindweft_workspace.mcp_resolution import *  # noqa: F403

_modules[__name__] = _import_module("mindweft_workspace.mcp_resolution")
