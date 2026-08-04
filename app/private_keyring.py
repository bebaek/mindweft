from __future__ import annotations

import base64
import binascii
import json
from collections.abc import Mapping


def load_encryption_keyring(
    env: Mapping[str, str],
    *,
    single_key_env: str,
    keyring_env: str,
    key_version_env: str,
    database_env: str,
) -> tuple[bytes, dict[int, bytes], int]:
    key_version = _positive_int(env.get(key_version_env, ""), key_version_env, 1)
    keys = _decode_keyring(env.get(keyring_env, ""), keyring_env)
    single_key = env.get(single_key_env, "").strip()
    if single_key:
        decoded = decode_encryption_key(single_key, single_key_env)
        existing = keys.get(key_version)
        if existing is not None and existing != decoded:
            raise RuntimeError(
                f"{single_key_env} conflicts with version {key_version} in {keyring_env}"
            )
        keys[key_version] = decoded
    active_key = keys.get(key_version)
    if active_key is None:
        raise RuntimeError(
            f"{single_key_env} or {keyring_env} is required to provide active key version "
            f"{key_version} when {database_env} is set"
        )
    return active_key, keys, key_version


def decode_encryption_key(raw: str, name: str) -> bytes:
    value = raw.strip()
    try:
        decoded = base64.urlsafe_b64decode((value + "=" * (-len(value) % 4)).encode("ascii"))
    except (binascii.Error, ValueError, UnicodeEncodeError) as exc:
        raise RuntimeError(f"{name} must be a base64-encoded 32-byte key") from exc
    if len(decoded) != 32:
        raise RuntimeError(f"{name} must be a base64-encoded 32-byte key")
    return decoded


def parse_boolean(raw: str, name: str, *, default: bool = False) -> bool:
    value = raw.strip().lower()
    if not value:
        return default
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise RuntimeError(f"{name} must be a boolean")


def _decode_keyring(raw: str, name: str) -> dict[int, bytes]:
    value = raw.strip()
    if not value:
        return {}
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{name} must be a JSON object mapping versions to keys") from exc
    if not isinstance(decoded, dict):
        raise RuntimeError(f"{name} must be a JSON object mapping versions to keys")
    keys: dict[int, bytes] = {}
    for raw_version, raw_key in decoded.items():
        try:
            version = int(raw_version)
        except (TypeError, ValueError) as exc:
            raise RuntimeError(f"{name} key versions must be positive integers") from exc
        if version < 1:
            raise RuntimeError(f"{name} key versions must be positive integers")
        if not isinstance(raw_key, str):
            raise RuntimeError(f"{name} values must be base64-encoded strings")
        keys[version] = decode_encryption_key(raw_key, name)
    return keys


def _positive_int(raw: str, name: str, default: int) -> int:
    if not raw.strip():
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be a positive integer") from exc
    if value < 1:
        raise RuntimeError(f"{name} must be a positive integer")
    return value
