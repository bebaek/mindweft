from __future__ import annotations

from app import agent_skills as legacy_agent_skills
from app import config as legacy_environment
from app import unified_config as legacy_unified_config
from app import unified_config_schema as legacy_schema
from minigent_config import agent_skills, environment, schema, unified_config


def test_legacy_agent_skill_module_reexports_canonical_helpers() -> None:
    assert legacy_agent_skills.AgentSkillMetadata is agent_skills.AgentSkillMetadata
    assert legacy_agent_skills.discover_agent_skills is agent_skills.discover_agent_skills
    assert legacy_agent_skills.load_agent_skill_body is agent_skills.load_agent_skill_body


def test_legacy_unified_config_module_reexports_canonical_helpers() -> None:
    assert legacy_unified_config.ResolvedConfig is unified_config.ResolvedConfig
    assert legacy_unified_config.resolve_unified_config is unified_config.resolve_unified_config
    assert legacy_unified_config.apply_startup_config is unified_config.apply_startup_config


def test_legacy_unified_config_schema_reexports_canonical_helpers() -> None:
    assert legacy_schema.UnifiedConfig is schema.UnifiedConfig
    assert legacy_schema.parse_unified_config is schema.parse_unified_config
    assert legacy_schema.validate_unified_config_data is schema.validate_unified_config_data


def test_legacy_environment_module_reexports_canonical_helper() -> None:
    assert legacy_environment.load_environment is environment.load_environment
