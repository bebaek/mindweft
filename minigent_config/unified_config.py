"""Legacy compatibility alias for :mod:`mindweft_config.unified_config`."""

from importlib import import_module as _import_module
from sys import modules as _modules

from mindweft_config.unified_config import *  # noqa: F403

_modules[__name__] = _import_module("mindweft_config.unified_config")
