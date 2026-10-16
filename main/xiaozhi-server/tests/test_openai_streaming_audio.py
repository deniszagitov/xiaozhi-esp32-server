import asyncio
import base64
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from core.providers.asr.dto.dto import InterfaceType as ASRInterfaceType
from core.providers.asr.openai_realtime import ASRProvider
from core.providers.llm.openai.openai import LLMProvider
from core.providers.tts.dto.dto import (
    InterfaceType as TTSInterfaceType,
    SentenceType as TTSSentenceType,
)
from core.providers.tts.openai_stream import TTSProvider
from core.utils.pcm_resampler import PCM16Resampler


class FakeWebSocket:
    def __init__(self, responses=None):
        self.sent = []
        self.responses = list(responses or [])
        self.closed = False

    async def send(self, message):
        self.sent.append(json.loads(message))

    async def recv(self):
        return json.dumps(self.responses.pop(0))

    async def close(self):
        self.closed = True


class FakeContent:
    def __init__(self, chunks, after_first_chunk=None):
        self.chunks = chunks
        self.after_first_chunk = after_first_chunk

    async def iter_any(self):
        for index, chunk in enumerate(self.chunks):
            yield chunk
            if index == 0 and self.after_first_chunk:
                self.after_first_chunk()


class FakeResponse:
    def __init__(self, chunks, status=200, after_first_chunk=None):
        self.status = status
        self.content = FakeContent(chunks, after_first_chunk)

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class FakeClientSession:
    last_request = None
    response = None

    def __init__(self, timeout=None):
        self.timeout = timeout

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    def post(self, url, json=None, headers=None):
        type(self).last_request = {
            "url": url,
            "json": json,
            "headers": headers,
        }
        return type(self).response


class FakeEncoder:
    def __init__(self, sample_rate=24000):
        self.sample_rate = sample_rate
        self.calls = []

    def encode_pcm_to_opus_stream(self, pcm, end_of_stream, callback):
        self.calls.append((pcm, end_of_stream))
        if pcm:
            callback(b"synthetic-opus")


class PCM16ResamplerTests(unittest.TestCase):
    def test_resamples_across_odd_transport_chunks(self):
        resampler = PCM16Resampler(16000, 24000)
        pcm = bytes(range(12))

        output = resampler.process(pcm[:3])
        output += resampler.process(pcm[3:])
        output += resampler.flush()

        self.assertEqual(len(output), 18)


class OpenAILLMTokenParameterTests(unittest.TestCase):
    def test_gpt5_uses_max_completion_tokens(self):
        provider = LLMProvider(
            {
                "api_key": "synthetic-key",
                "base_url": "https://api.openai.com/v1",
                "model_name": "gpt-5.4-nano",
            }
        )
        request = {}
        provider._apply_token_limit(request, 321)
        self.assertEqual(request, {"max_completion_tokens": 321})

    def test_legacy_models_keep_max_tokens(self):
        provider = LLMProvider(
            {
                "api_key": "synthetic-key",
                "base_url": "https://api.openai.com/v1",
                "model_name": "gpt-4.1-mini",
            }
        )
        request = {}
        provider._apply_token_limit(request, 321)
        self.assertEqual(request, {"max_tokens": 321})


class OpenAIRealtimeASRTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.provider = ASRProvider(
            {
                "api_key": "synthetic-key",
                "model": "gpt-realtime-whisper",
                "language": "ru",
                "delay": "low",
                "output_dir": "tmp/",
            },
            True,
        )

    async def test_uses_transcription_intent_endpoint_by_default(self):
        self.assertEqual(
            self.provider.api_url,
            "wss://api.openai.com/v1/realtime?intent=transcription",
        )

    async def test_sends_live_pcm_and_commits_local_vad_turn(self):
        websocket = FakeWebSocket()
        self.provider.asr_ws = websocket
        self.provider.is_processing = True
        self.provider._resampler = PCM16Resampler(16000, 24000)

        await self.provider._send_audio(b"\x00\x00" * 160)
        await self.provider._send_stop_request()

        self.assertEqual(
            [event["type"] for event in websocket.sent],
            [
                "input_audio_buffer.append",
                "input_audio_buffer.append",
                "input_audio_buffer.commit",
            ],
        )
        audio = b"".join(
            base64.b64decode(event["audio"])
            for event in websocket.sent
            if event["type"] == "input_audio_buffer.append"
        )
        self.assertEqual(len(audio), 160 * 3)

    async def test_receive_audio_commits_once_when_local_vad_stops(self):
        websocket = FakeWebSocket()
        self.provider.asr_ws = websocket
        self.provider.is_processing = True
        self.provider._resampler = PCM16Resampler(16000, 24000)
        conn = SimpleNamespace(client_voice_stop=True)

        with patch(
            "core.providers.asr.openai_realtime.ASRProviderBase.receive_audio",
            new=AsyncMock(),
        ):
            await self.provider.receive_audio(conn, b"\x00\x00" * 160, True)
            await self.provider.receive_audio(conn, b"\x00\x00" * 160, True)

        event_types = [event["type"] for event in websocket.sent]
        self.assertEqual(event_types.count("input_audio_buffer.commit"), 1)
        self.assertEqual(event_types[-1], "input_audio_buffer.commit")

    async def test_starts_transcription_session_without_server_vad(self):
        websocket = FakeWebSocket()
        conn = SimpleNamespace(asr_audio=[b"\x00\x00" * 160])

        def close_receiver(coroutine):
            coroutine.close()
            return SimpleNamespace(cancel=lambda: None)

        with patch(
            "core.providers.asr.openai_realtime.websockets.connect",
            new=AsyncMock(return_value=websocket),
        ), patch(
            "core.providers.asr.openai_realtime.asyncio.create_task",
            side_effect=close_receiver,
        ):
            await self.provider._start_recognition(conn)

        session = websocket.sent[0]["session"]
        audio_input = session["audio"]["input"]
        self.assertEqual(self.provider.interface_type, ASRInterfaceType.STREAM)
        self.assertEqual(session["type"], "transcription")
        self.assertEqual(audio_input["format"]["rate"], 24000)
        self.assertIsNone(audio_input["turn_detection"])
        self.assertEqual(
            audio_input["transcription"]["model"], "gpt-realtime-whisper"
        )

    async def test_completed_transcript_enters_existing_asr_lifecycle(self):
        websocket = FakeWebSocket(
            [
                {
                    "type": "conversation.item.input_audio_transcription.completed",
                    "transcript": "synthetic transcript",
                }
            ]
        )
        self.provider.asr_ws = websocket
        self.provider.is_processing = True
        self.provider.handle_voice_stop = AsyncMock()
        conn = SimpleNamespace(
            stop_event=asyncio.Event(),
            asr_audio=[b"\x00\x00" * 160],
            reset_audio_states=unittest.mock.Mock(),
        )

        await self.provider._forward_results(conn)

        self.provider.handle_voice_stop.assert_awaited_once_with(
            conn, conn.asr_audio
        )
        conn.reset_audio_states.assert_called_once()
        self.assertTrue(websocket.closed)

    async def test_cleanup_cancels_the_forwarding_task(self):
        websocket = FakeWebSocket()
        forwarding_task = asyncio.create_task(asyncio.Event().wait())
        self.provider.asr_ws = websocket
        self.provider.forward_task = forwarding_task

        await self.provider._cleanup()

        self.assertTrue(forwarding_task.cancelled())
        self.assertTrue(websocket.closed)


class OpenAIStreamingTTSTests(unittest.IsolatedAsyncioTestCase):
    async def test_streams_pcm_chunks_into_existing_opus_queue(self):
        provider = TTSProvider(
            {
                "api_key": "synthetic-key",
                "model": "gpt-4o-mini-tts",
                "voice": "alloy",
                "output_dir": "tmp/",
            },
            True,
        )
        encoder = FakeEncoder()
        provider.opus_encoder = encoder
        received_opus = []
        pcm = b"\x01\x00\x02\x00\x03\x00\x04\x00"
        FakeClientSession.response = FakeResponse([pcm[:3], pcm[3:]])

        with patch(
            "core.providers.tts.openai_stream.aiohttp.ClientSession",
            FakeClientSession,
        ):
            await provider._stream_pcm("synthetic text", received_opus.append)

        request = FakeClientSession.last_request
        self.assertEqual(provider.interface_type, TTSInterfaceType.SINGLE_STREAM)
        self.assertEqual(request["json"]["response_format"], "pcm")
        self.assertEqual(request["json"]["model"], "gpt-4o-mini-tts")
        self.assertEqual(b"".join(call[0] for call in encoder.calls), pcm)
        self.assertTrue(encoder.calls[-1][1])
        self.assertEqual(received_opus, [b"synthetic-opus", b"synthetic-opus"])


class OpenAIStreamingTTSPublicLifecycleTests(unittest.TestCase):
    def test_public_stream_outputs_opus_before_http_body_finishes(self):
        provider = TTSProvider(
            {
                "api_key": "synthetic-key",
                "model": "gpt-4o-mini-tts",
                "voice": "alloy",
                "output_dir": "tmp/",
            },
            True,
        )
        encoder = FakeEncoder()
        provider.opus_encoder = encoder
        observed_after_first_chunk = []

        def observe_first_chunk():
            observed_after_first_chunk.append(len(encoder.calls))

        FakeClientSession.response = FakeResponse(
            [b"\x01\x00\x02\x00", b"\x03\x00\x04\x00"],
            after_first_chunk=observe_first_chunk,
        )

        with patch(
            "core.providers.tts.openai_stream.aiohttp.ClientSession",
            FakeClientSession,
        ):
            provider.to_tts_stream("synthetic text")

        first_queue_item = provider.tts_audio_queue.get_nowait()
        self.assertEqual(observed_after_first_chunk, [1])
        self.assertEqual(first_queue_item[0], TTSSentenceType.FIRST)
        self.assertEqual(first_queue_item[2], "synthetic text")
        self.assertTrue(encoder.calls[-1][1])


if __name__ == "__main__":
    unittest.main()
