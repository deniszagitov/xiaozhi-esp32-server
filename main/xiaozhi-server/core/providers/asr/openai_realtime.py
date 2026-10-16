import asyncio
import base64
import json

import websockets

from config.logger import setup_logging
from core.providers.asr.base import ASRProviderBase
from core.providers.asr.dto.dto import InterfaceType
from core.utils.pcm_resampler import PCM16Resampler


TAG = __name__
logger = setup_logging()


class ASRProvider(ASRProviderBase):
    """OpenAI Realtime transcription using the repository's local VAD."""

    def __init__(self, config, delete_audio_file):
        super().__init__()
        self.interface_type = InterfaceType.STREAM
        self.api_key = config.get("api_key")
        self.model = config.get("model", "gpt-realtime-whisper")
        self.api_url = config.get(
            "api_url",
            "wss://api.openai.com/v1/realtime?intent=transcription",
        )
        self.language = config.get("language")
        self.delay = config.get("delay", "low")
        self.input_sample_rate = int(config.get("input_sample_rate", 16000))
        self.realtime_sample_rate = 24000
        self.output_dir = config.get("output_dir", "tmp/")
        self.delete_audio_file = delete_audio_file

        if not self.api_key:
            raise ValueError("OpenAI api_key is required")

        self.text = ""
        self.asr_ws = None
        self.forward_task = None
        self.is_processing = False
        self._commit_sent = False
        self._resampler = None

    async def receive_audio(self, conn, pcm_frame, audio_have_voice):
        await super().receive_audio(conn, pcm_frame, audio_have_voice)

        started = False
        if audio_have_voice and not self.is_processing and self.asr_ws is None:
            try:
                await self._start_recognition(conn)
                started = True
            except Exception as exc:
                logger.bind(tag=TAG).error(
                    f"Failed to start OpenAI Realtime transcription: {exc}"
                )
                await self._cleanup()
                return

        if self.asr_ws and self.is_processing and not self._commit_sent:
            try:
                if not started:
                    await self._send_audio(pcm_frame)
                if conn.client_voice_stop:
                    await self._send_stop_request()
            except Exception as exc:
                logger.bind(tag=TAG).warning(
                    f"Failed to send audio to OpenAI Realtime: {exc}"
                )
                await self._cleanup()

    async def _start_recognition(self, conn):
        self.asr_ws = await websockets.connect(
            self.api_url,
            additional_headers={"Authorization": f"Bearer {self.api_key}"},
            max_size=1000000000,
            ping_interval=20,
            ping_timeout=20,
            close_timeout=5,
        )
        self.is_processing = True
        self._commit_sent = False
        self.text = ""
        self._resampler = PCM16Resampler(
            self.input_sample_rate, self.realtime_sample_rate
        )

        transcription = {
            "model": self.model,
            "delay": self.delay,
        }
        if self.language:
            transcription["language"] = self.language

        session_update = {
            "type": "session.update",
            "session": {
                "type": "transcription",
                "audio": {
                    "input": {
                        "format": {
                            "type": "audio/pcm",
                            "rate": self.realtime_sample_rate,
                        },
                        "transcription": transcription,
                        "turn_detection": None,
                    }
                },
            },
        }
        await self.asr_ws.send(json.dumps(session_update))
        self.forward_task = asyncio.create_task(self._forward_results(conn))

        for cached_pcm in conn.asr_audio[-10:]:
            await self._send_audio(cached_pcm)

    async def _send_audio(self, pcm_frame):
        pcm_24khz = self._resampler.process(pcm_frame)
        if not pcm_24khz:
            return
        event = {
            "type": "input_audio_buffer.append",
            "audio": base64.b64encode(pcm_24khz).decode("ascii"),
        }
        await self.asr_ws.send(json.dumps(event))

    async def _send_stop_request(self):
        if not self.asr_ws or self._commit_sent:
            return

        tail = self._resampler.flush() if self._resampler else b""
        if tail:
            event = {
                "type": "input_audio_buffer.append",
                "audio": base64.b64encode(tail).decode("ascii"),
            }
            await self.asr_ws.send(json.dumps(event))

        self._commit_sent = True
        await self.asr_ws.send(
            json.dumps({"type": "input_audio_buffer.commit"})
        )

    async def _forward_results(self, conn):
        try:
            while self.asr_ws and not conn.stop_event.is_set():
                event = json.loads(await self.asr_ws.recv())
                event_type = event.get("type")

                if event_type == "conversation.item.input_audio_transcription.completed":
                    self.text = event.get("transcript", "")
                    audio_data = conn.asr_audio.copy()
                    if self.text:
                        await self.handle_voice_stop(conn, audio_data)
                    break
                if event_type == "conversation.item.input_audio_transcription.failed":
                    logger.bind(tag=TAG).error(
                        "OpenAI Realtime transcription failed"
                    )
                    break
                if event_type == "error":
                    error = event.get("error", {})
                    error_code = error.get("code", "unknown")
                    logger.bind(tag=TAG).error(
                        f"OpenAI Realtime transcription error: {error_code}"
                    )
                    break
        except websockets.ConnectionClosed:
            logger.bind(tag=TAG).info(
                "OpenAI Realtime transcription connection closed"
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.bind(tag=TAG).error(
                f"Failed to process OpenAI Realtime transcription: {exc}"
            )
        finally:
            await self._cleanup()
            conn.reset_audio_states()

    async def speech_to_text(self, opus_data, session_id, artifacts=None):
        result = self.text
        self.text = ""
        return result, None

    def stop_ws_connection(self):
        self.is_processing = False

    async def _cleanup(self):
        current_task = asyncio.current_task()
        forward_task = self.forward_task
        self.forward_task = None
        ws = self.asr_ws
        self.asr_ws = None

        self.is_processing = False
        self._commit_sent = False
        self._resampler = None

        if (
            forward_task
            and forward_task is not current_task
            and not forward_task.done()
        ):
            forward_task.cancel()

        if ws:
            try:
                await asyncio.wait_for(ws.close(), timeout=2)
            except Exception as exc:
                logger.bind(tag=TAG).debug(
                    f"Failed to close OpenAI Realtime connection: {exc}"
                )

        if forward_task and forward_task is not current_task:
            try:
                await forward_task
            except asyncio.CancelledError:
                pass

    async def close(self):
        await self._cleanup()
