"""Legacy compatibility alias for :mod:`mindweft_mcp.path_policy`."""

from importlib import import_module as _import_module
from sys import modules as _modules

from mindweft_mcp.path_policy import *  # noqa: F403

_modules[__name__] = _import_module("mindweft_mcp.path_policy")
