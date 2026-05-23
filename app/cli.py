from __future__ import annotations

import sys
import urllib.request  # noqa: F401 - exposed for existing CLI tests that monkeypatch urlopen.

from minigent_client.one_shot_cli import main

if __name__ == "__main__":
    sys.exit(main())
