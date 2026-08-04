from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

STATE_DIR_NAME = "minigent"
LEGACY_STATE_DIR_NAME = ".minigent"
STATE_FILE_NAME = "cli-state.json"
XDG_STATE_HOME_ENV = "XDG_STATE_HOME"
PROMPT_COMMANDS_KEY = "prompt_commands"


_PROMPT_COMMAND_NAME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_-]{0,63}$")


def state_dir_path() -> Path:
    configured_home = os.getenv(XDG_STATE_HOME_ENV, "").strip()
    if configured_home:
        state_home = Path(configured_home).expanduser()
        if state_home.is_absolute():
            return state_home / STATE_DIR_NAME
    return Path.home() / ".local" / "state" / STATE_DIR_NAME


def legacy_state_dir_path() -> Path:
    return Path.home() / LEGACY_STATE_DIR_NAME


def state_file_path() -> Path:
    return state_dir_path() / STATE_FILE_NAME


def legacy_state_file_path() -> Path:
    return legacy_state_dir_path() / STATE_FILE_NAME


@dataclass
class PromptCommand:
    name: str
    prompt_template: str
    description: str | None = None
    updated_at: str | None = None

    def to_dict(self) -> dict[str, str]:
        payload = {"prompt_template": self.prompt_template}
        if self.description:
            payload["description"] = self.description
        if self.updated_at:
            payload["updated_at"] = self.updated_at
        return payload


@dataclass
class ThreadHistoryItem:
    thread_id: str
    title: str | None = None
    updated_at: str | None = None
    message_count: int | None = None

    def to_dict(self) -> dict[str, str | int]:
        payload: dict[str, str | int] = {"thread_id": self.thread_id}
        if self.title:
            payload["title"] = self.title
        if self.updated_at:
            payload["updated_at"] = self.updated_at
        if self.message_count is not None:
            payload["message_count"] = self.message_count
        return payload


@dataclass
class ClientState:
    recent_threads: dict[str, str] = field(default_factory=dict)
    thread_history: dict[str, list[ThreadHistoryItem]] = field(default_factory=dict)
    path: Path = field(default_factory=state_file_path)
    extra: dict[str, object] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path | None = None) -> "ClientState":
        resolved_path = path or state_file_path()
        source_path = resolved_path
        if path is None and not resolved_path.exists():
            legacy_path = legacy_state_file_path()
            if legacy_path.exists():
                source_path = legacy_path
        try:
            data = json.loads(source_path.read_text())
        except FileNotFoundError:
            return cls(path=resolved_path)
        except json.JSONDecodeError:
            return cls(path=resolved_path)
        if not isinstance(data, dict):
            return cls(path=resolved_path)
        recent_threads = data.get("recent_threads", {})
        if not isinstance(recent_threads, dict):
            return cls(path=resolved_path)
        thread_history = data.get("thread_history", {})
        if not isinstance(thread_history, dict):
            thread_history = {}
        parsed_recent_threads = {
            str(key): value for key, value in recent_threads.items() if isinstance(value, str)
        }
        parsed_thread_history = _parse_thread_history(thread_history)
        for key, thread_id in parsed_recent_threads.items():
            if key not in parsed_thread_history:
                parsed_thread_history[key] = [ThreadHistoryItem(thread_id=thread_id)]
        state = cls(
            recent_threads=parsed_recent_threads,
            thread_history=parsed_thread_history,
            path=resolved_path,
            extra={
                str(key): value
                for key, value in data.items()
                if key not in {"recent_threads", "thread_history"}
            },
        )
        if source_path != resolved_path:
            try:
                state.save()
            except OSError:
                pass
        return state

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            **self.extra,
            "recent_threads": self.recent_threads,
            "thread_history": {
                key: [item.to_dict() for item in items]
                for key, items in self.thread_history.items()
            },
        }
        self.path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")

    def get_last_thread(self, key: str) -> str | None:
        thread_id = self.recent_threads.get(key)
        if not thread_id:
            return None
        return thread_id

    def set_last_thread(
        self,
        key: str,
        thread_id: str,
        *,
        title: str | None = None,
        updated_at: str | None = None,
        message_count: int | None = None,
    ) -> None:
        self.recent_threads[key] = thread_id
        self.remember_thread(
            key, thread_id, title=title, updated_at=updated_at, message_count=message_count
        )

    def remember_thread(
        self,
        key: str,
        thread_id: str,
        *,
        title: str | None = None,
        updated_at: str | None = None,
        message_count: int | None = None,
    ) -> None:
        items = [item for item in self.thread_history.get(key, []) if item.thread_id != thread_id]
        items.insert(
            0,
            ThreadHistoryItem(
                thread_id=thread_id,
                title=title,
                updated_at=updated_at or _utc_now_iso(),
                message_count=message_count,
            ),
        )
        self.thread_history[key] = items[:50]

    def list_threads(self, key: str) -> list[ThreadHistoryItem]:
        return list(self.thread_history.get(key, []))

    def rename_thread(self, key: str, thread_id: str, title: str) -> bool:
        changed = False
        for item in self.thread_history.get(key, []):
            if item.thread_id == thread_id:
                item.title = title
                item.updated_at = _utc_now_iso()
                changed = True
                break
        return changed

    def forget_last_thread(self, key: str, thread_id: str) -> bool:
        changed = False
        if self.recent_threads.get(key) == thread_id:
            del self.recent_threads[key]
            changed = True
        items = self.thread_history.get(key)
        if items is not None:
            filtered = [item for item in items if item.thread_id != thread_id]
            if len(filtered) != len(items):
                self.thread_history[key] = filtered
                changed = True
        return changed

    def list_prompt_commands(self) -> list[PromptCommand]:
        commands = self.extra.get(PROMPT_COMMANDS_KEY, {})
        if not isinstance(commands, dict):
            return []
        return sorted(_parse_prompt_commands(commands).values(), key=lambda command: command.name)

    def get_prompt_command(self, name: str) -> PromptCommand | None:
        normalized_name = normalize_prompt_command_name(name)
        commands = self.extra.get(PROMPT_COMMANDS_KEY, {})
        if not isinstance(commands, dict):
            return None
        return _parse_prompt_commands(commands).get(normalized_name)

    def set_prompt_command(
        self,
        name: str,
        prompt_template: str,
        *,
        description: str | None = None,
    ) -> PromptCommand:
        normalized_name = normalize_prompt_command_name(name)
        if not normalized_name:
            raise ValueError("command name is required")
        if _PROMPT_COMMAND_NAME_RE.fullmatch(normalized_name) is None:
            raise ValueError(
                "command names must start with a letter and contain only letters, numbers, '-', or '_'"
            )
        if not prompt_template.strip():
            raise ValueError("prompt template is required")
        raw_commands = self.extra.get(PROMPT_COMMANDS_KEY, {})
        commands = dict(raw_commands) if isinstance(raw_commands, dict) else {}
        command = PromptCommand(
            name=normalized_name,
            prompt_template=prompt_template.strip(),
            description=description.strip() if description and description.strip() else None,
            updated_at=_utc_now_iso(),
        )
        commands[normalized_name] = command.to_dict()
        self.extra[PROMPT_COMMANDS_KEY] = commands
        return command

    def delete_prompt_command(self, name: str) -> bool:
        normalized_name = normalize_prompt_command_name(name)
        raw_commands = self.extra.get(PROMPT_COMMANDS_KEY, {})
        if not isinstance(raw_commands, dict) or normalized_name not in raw_commands:
            return False
        commands = dict(raw_commands)
        del commands[normalized_name]
        if commands:
            self.extra[PROMPT_COMMANDS_KEY] = commands
        else:
            self.extra.pop(PROMPT_COMMANDS_KEY, None)
        return True


def normalize_prompt_command_name(name: str) -> str:
    return name.strip().removeprefix("/").lower()


def _parse_prompt_commands(raw_commands: dict[Any, Any]) -> dict[str, PromptCommand]:
    parsed: dict[str, PromptCommand] = {}
    for raw_name, raw_command in raw_commands.items():
        name = normalize_prompt_command_name(str(raw_name))
        if _PROMPT_COMMAND_NAME_RE.fullmatch(name) is None or not isinstance(raw_command, dict):
            continue
        prompt_template = raw_command.get("prompt_template")
        if not isinstance(prompt_template, str) or not prompt_template.strip():
            continue
        description = raw_command.get("description")
        updated_at = raw_command.get("updated_at")
        parsed[name] = PromptCommand(
            name=name,
            prompt_template=prompt_template,
            description=description if isinstance(description, str) else None,
            updated_at=updated_at if isinstance(updated_at, str) else None,
        )
    return parsed


def _parse_thread_history(raw_history: dict[Any, Any]) -> dict[str, list[ThreadHistoryItem]]:
    parsed: dict[str, list[ThreadHistoryItem]] = {}
    for key, raw_items in raw_history.items():
        if not isinstance(raw_items, list):
            continue
        items: list[ThreadHistoryItem] = []
        for raw_item in raw_items:
            if not isinstance(raw_item, dict):
                continue
            thread_id = raw_item.get("thread_id")
            if not isinstance(thread_id, str) or not thread_id:
                continue
            title = raw_item.get("title")
            updated_at = raw_item.get("updated_at")
            message_count = raw_item.get("message_count")
            items.append(
                ThreadHistoryItem(
                    thread_id=thread_id,
                    title=title if isinstance(title, str) else None,
                    updated_at=updated_at if isinstance(updated_at, str) else None,
                    message_count=message_count if isinstance(message_count, int) else None,
                )
            )
        if items:
            parsed[str(key)] = items
    return parsed


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def principal_key(
    *,
    api_token: str | None,
    user_id: str,
    tenant_id: str,
    is_admin: bool,
) -> str:
    if api_token:
        token_fingerprint = hashlib.sha256(api_token.encode("utf-8")).hexdigest()[:16]
        return f"bearer:{token_fingerprint}"
    return f"dev:{user_id}:{tenant_id}:{str(is_admin).lower()}"


def state_scope_key(
    base_url: str,
    *,
    api_token: str | None,
    user_id: str,
    tenant_id: str,
    is_admin: bool,
) -> str:
    principal = principal_key(
        api_token=api_token,
        user_id=user_id,
        tenant_id=tenant_id,
        is_admin=is_admin,
    )
    return f"{base_url.rstrip('/')}|{principal}"
