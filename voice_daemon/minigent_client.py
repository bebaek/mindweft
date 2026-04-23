from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from typing import Any, TextIO

from voice_daemon.config import VoiceDaemonConfig


class MinigentClient:
    def __init__(self, config: VoiceDaemonConfig, output_stream: TextIO | None = None) -> None:
        self._config = config
        self._thread_id = config.thread_id
        self._output_stream = output_stream or sys.stdout

    def ensure_thread(self) -> str:
        if self._thread_id:
            return self._thread_id
        payload = {"skill_name": self._config.skill_name} if self._config.skill_name else None
        response = self.request_json(
            "POST",
            f"{self._config.base_url}/threads",
            payload=payload,
        )
        thread_id = response["thread_id"]
        self._thread_id = thread_id
        return thread_id

    def send_user_message(self, content: str) -> dict[str, Any]:
        thread_id = self.ensure_thread()
        formatted_content = self._format_user_message(content)
        self._maybe_log_prompt(formatted_content)
        return self.request_json(
            "POST",
            f"{self._config.base_url}/threads/{thread_id}/messages",
            payload={"content": formatted_content},
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

    def _format_user_message(self, content: str) -> str:
        preamble = self._resolved_prompt_preamble()
        if preamble is None:
            return content
        return f"Client context:\n{preamble}\n\n{content}"

    def _maybe_log_prompt(self, content: str) -> None:
        if not self._config.debug_show_prompt:
            return
        self._output_stream.write(f"[prompt]\n{content}\n")
        self._output_stream.flush()

    def _resolved_prompt_preamble(self) -> str | None:
        if self._config.prompt_preamble:
            return self._config.prompt_preamble
        if self._config.location:
            return f"location={self._config.location}"
        return None
