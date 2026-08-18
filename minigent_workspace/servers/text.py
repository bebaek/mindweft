"""Legacy compatibility alias for :mod:`mindweft_workspace.servers.text`."""

from importlib import import_module as _import_module
from sys import modules as _modules

from mindweft_workspace.servers.text import *  # noqa: F403

_module = _import_module("mindweft_workspace.servers.text")
if __name__ == "__main__":
    raise SystemExit(_module.main())
_modules[__name__] = _module
