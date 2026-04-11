from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class PrincipalConfig:
    user_id: str
    tenant_id: str
    is_admin: bool = False
    api_token: str | None = None

    def build_headers(self) -> dict[str, str]:
        if self.api_token:
            return {"Authorization": f"Bearer {self.api_token}"}
        return {
            "X-Minigent-User-Id": self.user_id,
            "X-Minigent-Tenant-Id": self.tenant_id,
            "X-Minigent-Admin": "true" if self.is_admin else "false",
        }


@dataclass(frozen=True)
class VoiceDaemonConfig:
    base_url: str
    wake_phrase: str
    skill_name: str | None = None
    thread_id: str | None = None
    principal: PrincipalConfig = PrincipalConfig(user_id="demo-user", tenant_id="demo-tenant")

    @classmethod
    def from_env(cls) -> "VoiceDaemonConfig":
        return cls(
            base_url=os.getenv("MINIGENT_BASE_URL", "http://127.0.0.1:8000").rstrip("/"),
            wake_phrase=os.getenv("MINIGENT_VOICE_WAKE_PHRASE", "hey minigent").strip(),
            skill_name=_clean_optional(os.getenv("MINIGENT_VOICE_SKILL")),
            thread_id=_clean_optional(os.getenv("MINIGENT_VOICE_THREAD_ID")),
            principal=PrincipalConfig(
                user_id=os.getenv("MINIGENT_VOICE_USER_ID", "demo-user"),
                tenant_id=os.getenv("MINIGENT_VOICE_TENANT_ID", "demo-tenant"),
                is_admin=os.getenv("MINIGENT_VOICE_ADMIN", "").lower() in {"1", "true", "yes"},
                api_token=_clean_optional(os.getenv("MINIGENT_VOICE_API_TOKEN")),
            ),
        )


def _clean_optional(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None
