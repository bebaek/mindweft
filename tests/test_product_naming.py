from __future__ import annotations

from pathlib import Path

_EXPECTED_LEGACY_DOC_REFERENCES = {
    ".env.coding.template": 1,
    ".env.docker.template": 1,
    ".env.template": 1,
    "README.md": 3,
    "docs/cli.md": 1,
    "docs/coding-workspace.md": 2,
    "docs/mindweft-toml.md": 3,
    "docs/minigent-toml.md": 1,
    "docs/reference.md": 5,
    "local-agent-wrapper/README.md": 2,
    "minigent.toml.template": 1,
}


def test_public_docs_limit_minigent_environment_names_to_compatibility_boundaries() -> None:
    root = Path(__file__).parents[1]
    candidates = [
        root / "README.md",
        *(root / "docs").rglob("*.md"),
        root / "local-agent-wrapper" / "README.md",
        *root.glob("*.template"),
    ]
    actual = {
        str(path.relative_to(root)): count
        for path in candidates
        if (count := path.read_text(encoding="utf-8").count("MINIGENT_"))
    }

    assert actual == _EXPECTED_LEGACY_DOC_REFERENCES
