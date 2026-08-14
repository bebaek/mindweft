from __future__ import annotations

import json

_SECRET_KEY_PARTS = ("token", "secret", "key", "authorization", "password")


def mask_value(value: object) -> object:
    if value is None:
        return None
    if isinstance(value, str):
        return "<set>" if value else ""
    return "<set>"


def mask_secrets(value: object) -> object:
    if isinstance(value, dict):
        masked: dict[str, object] = {}
        for key, item in value.items():
            key_text = str(key)
            if key_text in {
                "MINIGENT_LLM_PROFILES",
                "MINIGENT_TENANT_EXECUTION_CONFIGS",
            } and isinstance(item, str):
                try:
                    masked[key_text] = json.dumps(mask_secrets(json.loads(item)), sort_keys=True)
                except json.JSONDecodeError:
                    masked[key_text] = "<set>"
            elif any(part in key_text.casefold() for part in _SECRET_KEY_PARTS):
                masked[key_text] = mask_value(item)
            else:
                masked[key_text] = mask_secrets(item)
        return masked
    if isinstance(value, list):
        return [mask_secrets(item) for item in value]
    return value
