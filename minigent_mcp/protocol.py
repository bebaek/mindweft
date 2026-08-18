"""Legacy compatibility alias for :mod:`mindweft_mcp.protocol`."""

from importlib import import_module as _import_module
from sys import modules as _modules

from mindweft_mcp.protocol import *  # noqa: F403

_modules[__name__] = _import_module("mindweft_mcp.protocol")
