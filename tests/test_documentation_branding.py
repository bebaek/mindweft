import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).parents[1]
DOCS_ROOT = PROJECT_ROOT / "docs"
LEGACY_PRODUCT_PATTERN = re.compile(r"\bMinigent\b")


def test_documentation_uses_mindweft_product_name_except_for_compatibility_notes() -> None:
    unexpected: list[str] = []
    for path in sorted(DOCS_ROOT.rglob("*.md")):
        for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if not LEGACY_PRODUCT_PATTERN.search(line):
                continue
            lowered = line.lower()
            if "legacy" in lowered or "compatibility" in lowered:
                continue
            unexpected.append(f"{path.relative_to(PROJECT_ROOT)}:{line_number}: {line}")

    assert unexpected == []


def test_documentation_uses_canonical_cli_commands() -> None:
    unexpected: list[str] = []
    for path in sorted(DOCS_ROOT.rglob("*.md")):
        for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if line.startswith(("minigent ", "minigent-client ")):
                unexpected.append(f"{path.relative_to(PROJECT_ROOT)}:{line_number}: {line}")

    assert unexpected == []
