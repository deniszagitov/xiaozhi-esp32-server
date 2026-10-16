import asyncio

import aiohttp

from config.logger import setup_logging
from core.providers.tts.base import TTSProviderBase
from core.providers.tts.dto.dto import InterfaceType, SentenceType
from core.utils.pcm_resampler import PCM16Resampler
from core.utils.tts import MarkdownCleaner
from core.utils.util import check_model_key


TAG = __name__
logger = setup_logging()


class TTSProvider(TTSProviderBase):
    TTS_PARAM_CONFIG = [
        ("ttsRate", "speed", 0.25, 4, 1, lambda value: round(float(value), 2)),
    ]

    def __init__(self, config, delete_audio_file):
        super().__init__(config, delete_audio_file)
        self.interface_type = InterfaceType.SINGLE_STREAM
        self.api_key = config.get("api_key")
        self.api_url = config.get(
            "api_url", "https://api.openai.com/v1/audio/speech"
        )
        self.model = config.get("model", "gpt-4o-mini-tts")
        self.voice = config.get("private_voice") or config.get("voice", "alloy")
        self.instructions = config.get("instructions")
        self.speed = float(config.get("speed") or 1.0)
        self._apply_percentage_params(config)
        # The inherited cache/file path needs a self-describing audio container.
        # The streaming path below still requests raw PCM.
        self.audio_file_type = "wav"
        self.source_sample_rate = 24000

        model_key_msg = check_model_key("TTS", self.api_key)
        if model_key_msg:
            logger.bind(tag=TAG).error(model_key_msg)

    def to_tts_stream(self, text, opus_handler=None):
        original_text = text
        text = MarkdownCleaner.clean_markdown(text)
        if self._correct_words_pattern:
            text = self._correct_words_pattern.sub(
                lambda match: self.correct_words[match.group(0)], text
            )
        if not text:
            return

        try:
            asyncio.run(
                self._stream_pcm(
                    text,
                    opus_handler or self.handle_opus,
                    display_text=original_text,
                )
            )
        except Exception as exc:
            logger.bind(tag=TAG).error(f"OpenAI streaming TTS request failed: {exc}")

    async def _stream_pcm(self, text, opus_handler, display_text=None):
        payload = self._build_payload(text)
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        timeout = aiohttp.ClientTimeout(total=self.tts_timeout)
        output_rate = self.opus_encoder.sample_rate
        resampler = PCM16Resampler(self.source_sample_rate, output_rate)

        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                self.api_url, json=payload, headers=headers
            ) as response:
                if response.status != 200:
                    raise RuntimeError(f"HTTP {response.status}")

                self.tts_audio_queue.put(
                    (
                        SentenceType.FIRST,
                        None,
                        display_text or text,
                        getattr(self, "current_sentence_id", None),
                    )
                )
                async for chunk in response.content.iter_any():
                    pcm = resampler.process(chunk)
                    if pcm:
                        self.opus_encoder.encode_pcm_to_opus_stream(
                            pcm,
                            end_of_stream=False,
                            callback=opus_handler,
                        )

                tail = resampler.flush()
                self.opus_encoder.encode_pcm_to_opus_stream(
                    tail,
                    end_of_stream=True,
                    callback=opus_handler,
                )

    def _build_payload(self, text):
        payload = {
            "model": self.model,
            "input": text,
            "voice": self.voice,
            "response_format": "pcm",
            "speed": self.speed,
        }
        if self.instructions:
            payload["instructions"] = self.instructions
        return payload

    async def text_to_speak(self, text, output_file):
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        timeout = aiohttp.ClientTimeout(total=self.tts_timeout)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                self.api_url,
                json={**self._build_payload(text), "response_format": "wav"},
                headers=headers,
            ) as response:
                if response.status != 200:
                    raise RuntimeError(f"OpenAI TTS HTTP {response.status}")
                audio = await response.read()

        if output_file:
            with open(output_file, "wb") as audio_file:
                audio_file.write(audio)
            return None
        return audio
