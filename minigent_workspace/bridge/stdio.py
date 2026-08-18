"""Legacy compatibility alias for :mod:`mindweft_workspace.bridge.stdio`."""

from importlib import import_module as _import_module
from sys import modules as _modules

from mindweft_workspace.bridge.stdio import *  # noqa: F403

_module = _import_module("mindweft_workspace.bridge.stdio")
if __name__ == "__main__":
    raise SystemExit(_module.main())
_modules[__name__] = _module
