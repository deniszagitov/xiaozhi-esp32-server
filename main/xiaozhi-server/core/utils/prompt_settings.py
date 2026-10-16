"""Database-backed conversation prompt settings."""

from collections.abc import Mapping
from typing import Any


PROMPT_SETTINGS_KEY = "assistant"
PROMPTS_KEY = "prompts"
EXIT_PROMPT_KEY = "exit"
GOODBYE_FALLBACK_KEY = "goodbye_fallback"


def get_prompt_settings(config: Mapping[str, Any] | None) -> Mapping[str, Any]:
    """Return the configured assistant prompt mapping, or an empty mapping."""

    if not isinstance(config, Mapping):
        return {}

    assistant_config = config.get(PROMPT_SETTINGS_KEY)
    if not isinstance(assistant_config, Mapping):
        return {}

    prompts = assistant_config.get(PROMPTS_KEY)
    return prompts if isinstance(prompts, Mapping) else {}


def get_prompt(
    config: Mapping[str, Any] | None, prompt_name: str, default: str = ""
) -> str:
    """Read one prompt value without putting prompt content in source code."""

    value = get_prompt_settings(config).get(prompt_name, default)
    return value.strip() if isinstance(value, str) else default
