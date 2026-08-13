from pathlib import Path

import pytest

from minigent_config.agent_skills import (
    discover_agent_skills,
    load_agent_skill_body,
    parse_agent_skill_metadata,
)


def test_parse_agent_skill_metadata_reads_frontmatter_only(tmp_path: Path) -> None:
    skill_dir = tmp_path / "code-reviewer"
    skill_dir.mkdir()
    skill_md = skill_dir / "SKILL.md"
    skill_md.write_text(
        "---\n"
        "name: code-reviewer\n"
        "description: Reviews code changes.\n"
        "allowed-tools: Read Grep\n"
        "---\n\n"
        "Review code carefully.\n",
        encoding="utf-8",
    )

    metadata = parse_agent_skill_metadata(skill_md)

    assert metadata.name == "code-reviewer"
    assert metadata.description == "Reviews code changes."
    assert metadata.allowed_tools == ("Read", "Grep")
    assert metadata.skill_md_path == skill_md
    assert metadata.skill_dir == skill_dir


def test_load_agent_skill_body_excludes_frontmatter(tmp_path: Path) -> None:
    skill_dir = tmp_path / "support"
    skill_dir.mkdir()
    skill_md = skill_dir / "SKILL.md"
    skill_md.write_text(
        "---\nname: support\ndescription: Helps support users.\n---\n\n"
        "Use empathy.\nAsk concise follow-up questions.\n",
        encoding="utf-8",
    )

    assert load_agent_skill_body(skill_md) == "Use empathy.\nAsk concise follow-up questions."


def test_parse_agent_skill_metadata_supports_list_allowed_tools_and_directory_name(
    tmp_path: Path,
) -> None:
    skill_dir = tmp_path / "docs writer"
    skill_dir.mkdir()
    skill_md = skill_dir / "SKILL.md"
    skill_md.write_text(
        "---\ndescription: Writes docs.\nallowed-tools:\n  - Read\n  - Grep\n---\n\nWrite docs.\n",
        encoding="utf-8",
    )

    metadata = parse_agent_skill_metadata(skill_md)

    assert metadata.name == "docs-writer"
    assert metadata.allowed_tools == ("Read", "Grep")


def test_discover_agent_skills_finds_child_skill_dirs(tmp_path: Path) -> None:
    (tmp_path / "one").mkdir()
    (tmp_path / "one" / "SKILL.md").write_text("---\nname: one\n---\n\nOne.\n", encoding="utf-8")
    (tmp_path / "not-a-skill").mkdir()

    assert [skill.name for skill in discover_agent_skills([tmp_path])] == ["one"]


def test_parse_agent_skill_metadata_rejects_unclosed_frontmatter(tmp_path: Path) -> None:
    skill_dir = tmp_path / "broken"
    skill_dir.mkdir()
    skill_md = skill_dir / "SKILL.md"
    skill_md.write_text("---\nname: broken\n\nBody.\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="missing a closing"):
        parse_agent_skill_metadata(skill_md)
