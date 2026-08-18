"""Legacy compatibility alias for :mod:`mindweft_client.config_commands`."""

from importlib import import_module as _import_module
from sys import modules as _modules

from mindweft_client.config_commands import *  # noqa: F403

_modules[__name__] = _import_module("mindweft_client.config_commands")
