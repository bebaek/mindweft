from __future__ import annotations

import fnmatch
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class MCPPathPolicy:
    deny_globs: list[str] = field(default_factory=list)
    allow_globs: list[str] = field(default_factory=list)


def iter_path_arguments(value: Any) -> list[str]:
    paths: list[str] = []
    if isinstance(value, dict):
        for key, nested in value.items():
            if key in {"path", "paths", "source", "destination", "target"}:
                paths.extend(_coerce_paths(nested))
            elif isinstance(nested, dict | list):
                paths.extend(iter_path_arguments(nested))
    elif isinstance(value, list):
        for item in value:
            paths.extend(iter_path_arguments(item))
    return paths


def path_denied(path: str, policy: MCPPathPolicy) -> bool:
    normalized = path.replace("\\", "/").rstrip("/")
    parts = [part for part in normalized.split("/") if part]
    if _matches_path_globs(normalized, parts, policy.allow_globs):
        return False
    return _matches_path_globs(normalized, parts, policy.deny_globs)


def filter_directory_listing_text(text: str, policy: MCPPathPolicy) -> str:
    kept_lines: list[str] = []
    hidden_count = 0
    for line in text.splitlines():
        name = line.rsplit(" ", 1)[-1].strip()
        if name and path_denied(name, policy):
            hidden_count += 1
            continue
        kept_lines.append(line)
    if hidden_count:
        kept_lines.append(
            f"[hidden {hidden_count} entr{'y' if hidden_count == 1 else 'ies'} by path policy]"
        )
    return "\n".join(kept_lines)


def _coerce_paths(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [item for item in value if isinstance(item, str)]
    return []


def _matches_path_globs(normalized: str, parts: list[str], patterns: list[str]) -> bool:
    candidates = {normalized, normalized.lstrip("/")}
    candidates.update(parts)
    candidates.update("/".join(parts[index:]) for index in range(len(parts)))
    expanded_patterns = set(patterns)
    expanded_patterns.update(
        pattern.removesuffix("/**") for pattern in patterns if pattern.endswith("/**")
    )
    return any(
        fnmatch.fnmatch(candidate, pattern) or fnmatch.fnmatch(f"/{candidate}", pattern)
        for pattern in expanded_patterns
        for candidate in candidates
    )
