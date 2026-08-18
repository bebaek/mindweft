from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

_FRONTMATTER_DELIMITER = "---"
_NAME_PATTERN = re.compile(r"[^A-Za-z0-9_.:-]+")


@dataclass(frozen=True)
class AgentSkillMetadata:
    name: str
    description: str | None
    allowed_tools: tuple[str, ...]
    skill_md_path: Path
    skill_dir: Path


def discover_agent_skills(dirs: Sequence[Path]) -> list[AgentSkillMetadata]:
    """Discover Claude/Agent Skill packages under local directories.

    Each direct child directory containing a SKILL.md is treated as one Agent Skill package.
    This deliberately returns metadata only; callers should use load_agent_skill_body() for
    progressive disclosure when a skill is selected or activated.
    """

    discovered: list[AgentSkillMetadata] = []
    for directory in dirs:
        root = directory.expanduser()
        if not root.exists():
            continue
        if not root.is_dir():
            raise RuntimeError(f"Agent Skill directory is not a directory: {root}")
        for child in sorted(root.iterdir(), key=lambda path: path.name):
            skill_md = child / "SKILL.md"
            if child.is_dir() and skill_md.is_file():
                discovered.append(parse_agent_skill_metadata(skill_md))
    return discovered


def parse_agent_skill_metadata(skill_md_path: Path) -> AgentSkillMetadata:
    path = skill_md_path.expanduser()
    if not path.is_file():
        raise RuntimeError(f"Agent Skill file does not exist: {path}")
    content = path.read_text(encoding="utf-8")
    frontmatter, _body = _split_frontmatter(content)
    metadata = _parse_frontmatter(frontmatter) if frontmatter is not None else {}
    raw_name = metadata.get("name")
    name = (
        _normalize_skill_name(str(raw_name))
        if raw_name is not None
        else _normalize_skill_name(path.parent.name)
    )
    if not name:
        raise RuntimeError(f"Agent Skill {path} must have a non-empty name")
    description = metadata.get("description")
    if description is not None:
        description = str(description).strip() or None
    allowed_tools = _parse_allowed_tools(
        metadata.get("allowed-tools", metadata.get("allowed_tools"))
    )
    return AgentSkillMetadata(
        name=name,
        description=description,
        allowed_tools=tuple(allowed_tools),
        skill_md_path=path,
        skill_dir=path.parent,
    )


def load_agent_skill_body(skill_md_path: Path) -> str:
    """Load the instruction body of a SKILL.md file, excluding frontmatter."""

    path = skill_md_path.expanduser()
    if not path.is_file():
        raise RuntimeError(f"Agent Skill file does not exist: {path}")
    content = path.read_text(encoding="utf-8")
    _frontmatter, body = _split_frontmatter(content)
    body = body.strip()
    if not body:
        raise RuntimeError(f"Agent Skill {path} has an empty instruction body")
    return body


def _split_frontmatter(content: str) -> tuple[str | None, str]:
    normalized = content.replace("\r\n", "\n").replace("\r", "\n")
    if not normalized.startswith(_FRONTMATTER_DELIMITER + "\n"):
        return None, normalized
    rest = normalized[len(_FRONTMATTER_DELIMITER) + 1 :]
    closing_match = re.search(r"^---\s*$", rest, flags=re.MULTILINE)
    if closing_match is None:
        raise RuntimeError("Agent Skill frontmatter is missing a closing '---' delimiter")
    frontmatter = rest[: closing_match.start()]
    body = rest[closing_match.end() :]
    if body.startswith("\n"):
        body = body[1:]
    return frontmatter, body


def _parse_frontmatter(frontmatter: str) -> dict[str, Any]:
    """Parse the YAML subset used by Agent Skill frontmatter.

    This intentionally supports the common scalar and list forms without adding a YAML
    dependency: key: value, key: [a, b], and key followed by indented dash items.
    """

    parsed: dict[str, Any] = {}
    lines = frontmatter.split("\n")
    index = 0
    while index < len(lines):
        raw_line = lines[index]
        line = raw_line.strip()
        index += 1
        if not line or line.startswith("#"):
            continue
        if ":" not in line:
            raise RuntimeError(f"Unsupported Agent Skill frontmatter line: {raw_line}")
        key, value = line.split(":", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            raise RuntimeError(f"Unsupported Agent Skill frontmatter line: {raw_line}")
        if value:
            parsed[key] = _parse_frontmatter_value(value)
            continue
        items: list[str] = []
        while index < len(lines):
            item_line = lines[index]
            stripped = item_line.strip()
            if not stripped or stripped.startswith("#"):
                index += 1
                continue
            if not item_line.startswith((" ", "\t")) or not stripped.startswith("-"):
                break
            item = stripped[1:].strip()
            if item:
                items.append(_strip_quotes(item))
            index += 1
        parsed[key] = items
    return parsed


def _parse_frontmatter_value(value: str) -> str | list[str]:
    if value.startswith("[") and value.endswith("]"):
        inner = value[1:-1].strip()
        if not inner:
            return []
        return [_strip_quotes(part.strip()) for part in inner.split(",") if part.strip()]
    return _strip_quotes(value)


def _parse_allowed_tools(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    raw = str(value).strip()
    if not raw:
        return []
    if "," in raw:
        return [part.strip() for part in raw.split(",") if part.strip()]
    return [part.strip() for part in raw.split() if part.strip()]


def _strip_quotes(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        return value[1:-1]
    return value


def _normalize_skill_name(value: str) -> str:
    normalized = _NAME_PATTERN.sub("-", value.strip())
    return normalized.strip("-")
