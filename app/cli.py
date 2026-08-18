"""Compatibility facade for the packaged Mindweft CLI."""

import sys

from minigent_client.application import main
from minigent_client.one_shot_cli import urllib

__all__ = ["main", "urllib"]

if __name__ == "__main__":
    sys.exit(main())
