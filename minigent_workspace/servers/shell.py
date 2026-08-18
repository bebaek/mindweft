"""Legacy compatibility alias for :mod:`mindweft_workspace.servers.shell`."""

from importlib import import_module as _import_module
from sys import modules as _modules

from mindweft_workspace.servers.shell import *  # noqa: F403

_module = _import_module("mindweft_workspace.servers.shell")
if __name__ == "__main__":
    raise SystemExit(_module.main())
_modules[__name__] = _module
