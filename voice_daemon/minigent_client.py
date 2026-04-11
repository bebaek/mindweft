from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

from voice_daemon.config import VoiceDaemonConfig


class MinigentClient:
    def __init__(self, config: VoiceDaemonConfig) -> None:
        self._config = config

    def ensure_thread(self) -> str:
        if self._config.thread_id:
            return self._config.thread_id
        payload = {"skill_name": self._config.skill_name} if self._config.skill_name else None
        response = self.request_json(
            "POST",
            f"{self._config.base_url}/threads",
            payload=payload,
        )
        thread_id = response["thread_id"]
        object.__setattr__(self._config, "thread_id", thread_id)
        return thread_id

    def send_user_message(self, content: str) -> dict[str, Any]:
        thread_id = self.ensure_thread()
        return self.request_json(
            "POST",
            f"{self._config.base_url}/threads/{thread_id}/messages",
            payload={"content": content},
        )

    def run_thread(self) -> str:
        thread_id = self.ensure_thread()
        response = self.request_json(
            "POST",
            f"{self._config.base_url}/threads/{thread_id}/run",
        )
        reply = response["reply"]
        if not isinstance(reply, str):
            raise RuntimeError("Minigent reply must be a string")
        return reply

    def request_json(
        self,
        method: str,
        url: str,
        *,
        payload: dict[str, Any] | None = None,
    ) -> Any:
        data = None
        headers = self._config.principal.build_headers()
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"

        request = urllib.request.Request(url, method=method, data=data, headers=headers)
        try:
            with urllib.request.urlopen(request) as response:
                raw_body = response.read().decode("utf-8")
                if not raw_body:
                    return None
                return json.loads(raw_body)
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"{method} {url} failed: {exc.code} {body}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"{method} {url} failed: {exc.reason}") from exc
