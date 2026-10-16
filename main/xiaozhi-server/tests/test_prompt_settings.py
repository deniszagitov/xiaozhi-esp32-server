import unittest

from core.utils.prompt_manager import get_conversation_language
from core.utils.prompt_settings import get_prompt, get_prompt_settings


class PromptSettingsTest(unittest.TestCase):
    def test_reads_database_backed_prompt_mapping(self):
        config = {
            "assistant": {
                "prompts": {
                    "exit": "  Farewell in the configured language.  ",
                    "goodbye_fallback": "Goodbye.",
                }
            }
        }

        self.assertEqual(
            {"exit": "  Farewell in the configured language.  ", "goodbye_fallback": "Goodbye."},
            dict(get_prompt_settings(config)),
        )
        self.assertEqual(
            "Farewell in the configured language.", get_prompt(config, "exit")
        )
        self.assertEqual("Goodbye.", get_prompt(config, "goodbye_fallback"))

    def test_missing_or_malformed_settings_are_safe(self):
        self.assertEqual({}, get_prompt_settings({"assistant": None}))
        self.assertEqual("fallback", get_prompt({}, "title", "fallback"))
        self.assertEqual("fallback", get_prompt({"assistant": {"prompts": []}}, "title", "fallback"))

    def test_conversation_language_is_not_taken_from_tts(self):
        self.assertEqual(
            "Русский",
            get_conversation_language(
                {
                    "conversation_language": " Русский ",
                    "TTS": {"TTS_OpenAIStreamTTS": {"language": "Multilingual"}},
                }
            ),
        )
        self.assertIsNone(
            get_conversation_language(
                {
                    "TTS": {"TTS_OpenAIStreamTTS": {"language": "中文"}},
                }
            )
        )


if __name__ == "__main__":
    unittest.main()
