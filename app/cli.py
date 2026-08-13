"""Compatibility facade for the packaged Minigent CLI."""

import sys

from minigent_client.one_shot_cli import main, urllib

__all__ = ["main", "urllib"]

if __name__ == "__main__":
    sys.exit(main())
