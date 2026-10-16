"""Live voice behavior at the cloud WebSocket and device audio seams."""

import asyncio
import base64
import json
import unittest
from unittest.mock import patch

from core.providers.live.openai_live import LiveConfig, LiveSession


class CloudSocket:
    def __init__(self):
        self.incoming = asyncio.Queue()
        self.sent = asyncio.Queue()
        self.events = []
        self.closed = False

    async def send(self, payload):
        event = json.loads(payload)
        self.events.append(event)
        await self.sent.put(event)
        if event["type"] == "session.close":
            await self.incoming.put({"type": "session.closed", "usage": {"seconds": 3.25}})

    async def recv(self):
        return json.dumps(await self.incoming.get())

    async def close(self):
        self.closed = True

    async def receive(self, event):
        await self.incoming.put(event)

    async def next_sent(self, kind):
        async def find():
            while True:
                event = await self.sent.get()
                if event["type"] == kind:
                    return event
        return await asyncio.wait_for(find(), 1)


class LiveVoiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # The repository's API mock layer replaces asyncio.sleep with a no-op.
        # Real pacing needs cooperative scheduling, without enabling live APIs.
        sleep = patch("asyncio.sleep", asyncio.tasks.sleep)
        sleep.start()
        self.addCleanup(sleep.stop)
        self.cloud = CloudSocket()
        self.device = []

        async def connect(url, **kwargs):
            self.assertEqual(url, "wss://api.openai.com/v1/live/sessions")
            self.assertEqual(kwargs["additional_headers"]["Authorization"], "Bearer synthetic-key")
            return self.cloud

        async def control(state):
            self.device.append(("control", state))

        async def audio(packet):
            self.device.append(("audio", packet))

        self.session = LiveSession(
            LiveConfig(api_key="synthetic-key", input_buffer_ms=2000, silence_ms=100, playback_tail_ms=20, start_lead_ms=0),
            send_audio=audio, send_control=control, connect=connect,
        )
        self.addAsyncCleanup(self.session.close)

    async def start(self):
        task = asyncio.create_task(self.session.start())
        start = await self.cloud.next_sent("session.start")
        await self.cloud.receive({"type": "session.started", "session": {"id": "live_test"}})
        await task
        return start

    async def test_ready_precedes_audio_and_pcm_is_preserved(self):
        pcm = b"\xe8\x03" * 320
        self.session.feed_pcm(pcm)
        task = asyncio.create_task(self.session.start())
        start = await self.cloud.next_sent("session.start")
        self.assertEqual(start["session"]["model"], "gpt-live-1")
        self.assertEqual(start["session"]["audio"]["format"], {"type": "audio/pcm", "rate": 16000})
        await asyncio.sleep(0.03)
        self.assertEqual([event["type"] for event in self.cloud.events], ["session.start"])
        await self.cloud.receive({"type": "session.started", "session": {"id": "live_test"}})
        await task
        frame = await self.cloud.next_sent("session.input_audio.append")
        self.assertEqual(base64.b64decode(frame["audio"]), pcm)
        silence = await self.cloud.next_sent("session.input_audio.append")
        self.assertEqual(base64.b64decode(silence["audio"]), b"\x00" * 640)

    async def test_ambient_pcm_does_not_keep_a_paid_session_open(self):
        idle = asyncio.Event()

        async def on_idle():
            idle.set()

        self.session.on_idle = on_idle
        from dataclasses import replace
        self.session.config = replace(self.session.config, idle_timeout=0.06)
        await self.start()
        for _ in range(5):
            self.session.feed_pcm(b"\xe8\x03" * 320)
            await asyncio.sleep(0.02)
        self.assertTrue(idle.is_set())

    async def test_speech_is_bracketed_by_device_controls_and_has_an_opus_tail(self):
        await self.start()
        pcm = (b"\x10\x27\xf0\xd8" * 160) * 3
        await self.cloud.receive({"type": "session.output_audio.delta", "delta": base64.b64encode(pcm).decode()})
        await self.cloud.receive({"type": "session.output_audio.delta", "delta": base64.b64encode(b"\x00" * 3840).decode()})
        for _ in range(100):
            if ("control", "stop") in self.device:
                break
            await asyncio.sleep(0.01)
        self.assertEqual(self.device[0], ("control", "start"))
        self.assertEqual(self.device[-1], ("control", "stop"))
        packets = [packet for kind, packet in self.device if kind == "audio"]
        self.assertTrue(packets)
        self.assertTrue(all(isinstance(packet, bytes) and packet for packet in packets))

    async def test_close_consumes_final_usage_and_prevents_late_playback(self):
        await self.start()
        await self.session.close()
        self.assertTrue(self.cloud.closed)
        self.assertEqual(self.session.usage_seconds, 3.25)
        before = list(self.device)
        await self.cloud.receive({"type": "session.output_audio.delta", "delta": base64.b64encode(b"\x01\x01" * 960).decode()})
        await asyncio.sleep(0.03)
        self.assertEqual(self.device, before)

    async def test_input_overflow_and_odd_pcm_are_explicit_errors(self):
        with self.assertRaises(ValueError):
            self.session.feed_pcm(b"\x01")
        with self.assertRaises(BufferError):
            self.session.feed_pcm(b"\x00" * 100000)

    async def test_invalid_base64_fails_session_instead_of_playing_garbage(self):
        await self.start()
        await self.cloud.receive({"type": "session.output_audio.delta", "delta": "!!!"})
        error = await asyncio.wait_for(self.session.wait_error(), 1)
        self.assertIsInstance(error, ValueError)
        self.assertFalse(any(kind == "audio" for kind, _ in self.device))
