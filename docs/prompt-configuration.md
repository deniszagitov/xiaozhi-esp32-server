# Conversation prompt configuration

Conversation prompts are data, not provider-specific code. In Manager mode they are stored in the shared `sys_params` table and edited from **Parameter Management**.

The migration creates these parameters:

| Parameter code | Purpose |
| --- | --- |
| `assistant.prompts.exit` | Instructions for calling `handle_exit_intent` and producing `say_goodbye` in the configured conversation language. |
| `assistant.prompts.title` | Instructions for generating a localized chat title. The value must contain `{conversation}`. |
| `assistant.prompts.summary` | Instructions for localized long-term memory summaries. The value may contain `{history_memory}` and `{conversation}`. |
| `assistant.prompts.goodbye_fallback` | Fallback text used only when the model omits `say_goodbye`. |

The values use the `text` parameter type so the Manager editor presents a multiline field. The parameters are global by design and apply to all agents. The selected agent language is still supplied by the normal agent/TTS configuration, so a single prompt can serve Russian, English, Chinese, and other agents.

New connections receive the current values through the agent configuration response. Existing connections keep their already-built system prompt until they reconnect.

Standalone deployments without Manager can omit these parameters; the server then avoids injecting a localized exit instruction and does not invent a language-specific goodbye. Manager installations receive the defaults through Liquibase.
