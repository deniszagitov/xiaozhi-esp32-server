-- liquibase formatted sql

-- changeset DenisZagitov:202607311600
INSERT INTO `sys_params` (`id`, `param_code`, `param_value`, `value_type`, `param_type`, `remark`)
SELECT 503,
       'assistant.prompts.exit',
       'When the user clearly ends the conversation, call handle_exit_intent. The say_goodbye argument must be a short, natural farewell in the configured conversation language.',
       'text',
       1,
       'Prompt for localized conversation exit handling'
WHERE NOT EXISTS (SELECT 1 FROM `sys_params` WHERE `param_code` = 'assistant.prompts.exit');

INSERT INTO `sys_params` (`id`, `param_code`, `param_value`, `value_type`, `param_type`, `remark`)
SELECT 504,
       'assistant.prompts.title',
       'Create a concise conversation title in the primary language used by the conversation. Return only the title, without explanations or punctuation. Keep it within 15 words. Conversation: {conversation}',
       'text',
       1,
       'Prompt for localized chat titles'
WHERE NOT EXISTS (SELECT 1 FROM `sys_params` WHERE `param_code` = 'assistant.prompts.title');

INSERT INTO `sys_params` (`id`, `param_code`, `param_value`, `value_type`, `param_type`, `remark`)
SELECT 505,
       'assistant.prompts.summary',
       'Summarize the conversation for long-term memory. Preserve important user information, merge it with previous memory, omit device-control results, weather and date facts, farewells, and filler, and return only the summary under 1800 characters. Previous memory: {history_memory} New conversation: {conversation}',
       'text',
       1,
       'Prompt for localized conversation memory summaries'
WHERE NOT EXISTS (SELECT 1 FROM `sys_params` WHERE `param_code` = 'assistant.prompts.summary');

INSERT INTO `sys_params` (`id`, `param_code`, `param_value`, `value_type`, `param_type`, `remark`)
SELECT 506,
       'assistant.prompts.goodbye_fallback',
       'Goodbye.',
       'text',
       1,
       'Fallback goodbye when the model omits say_goodbye'
WHERE NOT EXISTS (SELECT 1 FROM `sys_params` WHERE `param_code` = 'assistant.prompts.goodbye_fallback');

-- rollback DELETE FROM `sys_params` WHERE `param_code` IN ('assistant.prompts.exit', 'assistant.prompts.title', 'assistant.prompts.summary', 'assistant.prompts.goodbye_fallback');
