from __future__ import annotations

import json

import pytest

from app.mcp import MCPSettings


def _settings_with_policy(policy_fields: dict[str, object]) -> MCPSettings:
    server = {
        "name": "demo",
        "url": "https://example.com/mcp",
        **policy_fields,
    }
    return MCPSettings.from_env({"MINIGENT_MCP_SERVERS": json.dumps([server])})


def test_mcp_private_value_policy_defaults_to_deny() -> None:
    config = _settings_with_policy({}).servers[0]

    assert config.private_value_policy.mode == "deny"
    assert config.private_value_policy.argument_paths == ()
    assert config.private_value_tool_policies == {}


def test_mcp_private_value_policy_parses_per_tool_selected_paths() -> None:
    config = _settings_with_policy(
        {
            "private_value_policy": "deny",
            "private_value_tool_policies": {
                "send": {
                    "mode": "resolve_selected",
                    "argument_paths": ["recipient.email", "cc[*].email"],
                },
                "inspect": "pass_through",
            },
        }
    ).servers[0]

    assert config.private_value_policy.mode == "deny"
    assert config.private_value_tool_policies["send"].mode == "resolve_selected"
    assert config.private_value_tool_policies["send"].argument_paths == (
        "recipient.email",
        "cc[*].email",
    )
    assert config.private_value_tool_policies["inspect"].mode == "pass_through"


@pytest.mark.parametrize(
    "policy_fields",
    [
        {
            "private_value_policy": {
                "mode": "resolve_selected",
                "argument_paths": [],
            }
        },
        {
            "private_value_policy": {
                "mode": "deny",
                "argument_paths": ["recipient.email"],
            }
        },
        {
            "private_value_tool_policies": {
                "send": {
                    "mode": "resolve_selected",
                    "argument_paths": ["recipient..email"],
                }
            }
        },
        {"private_value_policy": "resolve_all"},
    ],
)
def test_mcp_private_value_policy_rejects_invalid_configuration(
    policy_fields: dict[str, object],
) -> None:
    with pytest.raises(RuntimeError, match="private_value_policy"):
        _settings_with_policy(policy_fields)
