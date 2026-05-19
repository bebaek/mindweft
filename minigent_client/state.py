from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path

STATE_DIR_NAME = ".minigent"
STATE_FILE_NAME = "cli-state.json"


def state_file_path() -> Path:
    return Path.home() / STATE_DIR_NAME / STATE_FILE_NAME


@dataclass
class ClientState:
    recent_threads: dict[str, str] = field(default_factory=dict)
    path: Path = field(default_factory=state_file_path)
    extra: dict[str, object] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path | None = None) -> "ClientState":
        resolved_path = path or state_file_path()
        try:
            data = json.loads(resolved_path.read_text())
        except FileNotFoundError:
            return cls(path=resolved_path)
        except json.JSONDecodeError:
            return cls(path=resolved_path)
        if not isinstance(data, dict):
            return cls(path=resolved_path)
        recent_threads = data.get("recent_threads", {})
        if not isinstance(recent_threads, dict):
            return cls(path=resolved_path)
        return cls(
            recent_threads={
                str(key): value for key, value in recent_threads.items() if isinstance(value, str)
            },
            path=resolved_path,
            extra={str(key): value for key, value in data.items() if key != "recent_threads"},
        )

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {**self.extra, "recent_threads": self.recent_threads}
        self.path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")

    def get_last_thread(self, key: str) -> str | None:
        thread_id = self.recent_threads.get(key)
        if not thread_id:
            return None
        return thread_id

    def set_last_thread(self, key: str, thread_id: str) -> None:
        self.recent_threads[key] = thread_id

    def forget_last_thread(self, key: str, thread_id: str) -> bool:
        if self.recent_threads.get(key) != thread_id:
            return False
        del self.recent_threads[key]
        return True


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
