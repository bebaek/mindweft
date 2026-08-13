from __future__ import annotations

from app import cli as legacy_cli
from minigent_client import one_shot_cli


def test_legacy_cli_reexports_canonical_entrypoint_and_urllib_module() -> None:
    assert legacy_cli.main is one_shot_cli.main
    assert legacy_cli.urllib is one_shot_cli.urllib
