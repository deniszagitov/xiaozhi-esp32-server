"""GPT-Live primary WebSocket with bounded PCM and stock Xiaozhi Opus playback."""

import asyncio
import base64
import binascii
from collections import deque
from dataclasses import dataclass, field
import json
import math
import time

import numpy as np
import opuslib_next
import websockets


@dataclass(frozen=True)
class LiveConfig:
    api_key: str = field(repr=False)
    model: str = "gpt-live-1"
    voice: str = "marin"
    instructions: str = (
        "Respond concisely and naturally in the user's language. Remain silent while the user speaks; "
        "do not backchannel. After a wake word, say you are listening. "
        "Delegate every request requiring reasoning, memory, current information or device "
        "control to the backend. Delegate farewells and requests to end the conversation "
        "so the backend can close the session. Never invent device state or claim an action succeeded "
        "without a verified backend result. Do not read tool names, JSON or emojis aloud."
    )
    url: str = "wss://api.openai.com/v1/live/sessions"
    input_buffer_ms: int = 6000
    output_buffer_ms: int = 3000
    silence_ms: int = 500
    playback_tail_ms: int = 120
    start_lead_ms: int = 40
    speech_threshold: int = 64
    ready_timeout: float = 15
    close_timeout: float = 4
    idle_timeout: float = 90

    def __post_init__(self):
        if not self.api_key or not self.model or not self.voice:
            raise ValueError("Live requires an API key, model and voice")
        if not self.url.startswith("wss://"):
            raise ValueError("Live requires a secure WebSocket URL")
        for name in ("input_buffer_ms", "output_buffer_ms", "silence_ms"):
            value = getattr(self, name)
            if not isinstance(value, int) or not 20 <= value <= 10000:
                raise ValueError(f"Invalid Live {name}")
        for name in ("ready_timeout", "close_timeout", "idle_timeout"):
            if not math.isfinite(getattr(self, name)) or getattr(self, name) <= 0:
                raise ValueError(f"Invalid Live {name}")
        if not 0 <= self.playback_tail_ms <= 1000 or not 0 <= self.start_lead_ms <= 1000:
            raise ValueError("Invalid Live playback timing")
        if not 0 <= self.speech_threshold <= 32767:
            raise ValueError("Invalid Live speech threshold")


class LiveSession:
    """One generation: start, feed PCM, receive device callbacks, then close.

    All calls belong to one asyncio loop. Audio uses mono PCM16LE at 16 kHz;
    device packets are Opus at that rate with 60-ms frames. There is no global
    voice engine registry and no dependency on the cascade ASR/TTS workers.
    """

    SAMPLE_RATE = 16000
    INPUT_FRAME_BYTES = 640  # 20 ms
    OPUS_FRAME_BYTES = 1920  # 60 ms

    def __init__(self, config, *, send_audio, send_control, connect=None,
                 on_transcript=None, on_delegation=None, on_idle=None):
        self.config = config
        self.send_audio = send_audio
        self.send_control = send_control
        self.on_transcript = on_transcript
        self.on_delegation = on_delegation
        self.on_idle = on_idle
        self.connect = connect or websockets.connect
        self.usage_seconds = 0.0
        self.session_id = None
        self._socket = None
        self._ready = asyncio.Event()
        self._finalized = asyncio.Event()
        self._error_event = asyncio.Event()
        self._error = None
        self._input = bytearray()
        self._output_partial = bytearray()
        self._pre_roll = deque(maxlen=3)
        self._output = asyncio.Queue(maxsize=max(1, config.output_buffer_ms // 20))
        self._gate_active = False
        self._quiet_ms = 0
        self._speaking = False
        self._listening = True
        self._last_voice = time.monotonic()
        self._send_lock = asyncio.Lock()
        self._start_lock = asyncio.Lock()
        self._tasks = []
        self._stopping = False
        self._close_task = None
        self._encoder = opuslib_next.Encoder(16000, 1, opuslib_next.APPLICATION_AUDIO)

    async def start(self):
        async with self._start_lock:
            if self._ready.is_set():
                return
            if self._stopping:
                raise RuntimeError("Live session is closed")
            try:
                self._socket = await self.connect(
                    self.config.url,
                    additional_headers={"Authorization": "Bearer " + self.config.api_key},
                    open_timeout=self.config.ready_timeout, max_size=1048576,
                    max_queue=8, ping_interval=20, close_timeout=2,
                )
                self._tasks.append(asyncio.create_task(self._read(), name="live-reader"))
                await self._send({"type": "session.start", "session": {
                    "model": self.config.model, "instructions": self.config.instructions,
                    "audio": {"format": {"type": "audio/pcm", "rate": 16000},
                              "output": {"voice": self.config.voice}},
                    "delegation": {"type": "client"},
                }})
                await asyncio.wait_for(self._ready.wait(), self.config.ready_timeout)
                if self._error:
                    raise self._error
                self._tasks.extend([
                    asyncio.create_task(self._pump_input(), name="live-input"),
                    asyncio.create_task(self._play_output(), name="live-playback"),
                ])
            except BaseException:
                await self.close()
                raise

    def feed_pcm(self, pcm):
        if len(pcm) % 2:
            raise ValueError("PCM must contain complete 16-bit samples")
        if self._stopping:
            raise RuntimeError("Live session is closed")
        if len(self._input) + len(pcm) > self.config.input_buffer_ms * 32:
            raise BufferError("Live microphone buffer is full")
        if self._speaking or not self._listening:
            return
        self._input.extend(pcm)

    def set_listening(self, enabled):
        self._listening = bool(enabled)
        if not enabled:
            self._input.clear()

    async def append_result(self, delegation_id, content):
        if self._stopping:
            return
        if not isinstance(content, str) or len(content.encode("utf-8")) > 480:
            raise ValueError("Live result must be a short plain string")
        await self._send({"type": "session.commentary.append",
                          "delegation_id": delegation_id, "content": content})

    async def wait_error(self):
        await self._error_event.wait()
        return self._error

    async def close(self):
        if self._close_task is None:
            self._close_task = asyncio.create_task(self._shutdown(), name="live-close")
        await asyncio.shield(self._close_task)

    async def _send(self, event):
        async with self._send_lock:
            await asyncio.wait_for(self._socket.send(json.dumps(event)), 5)

    async def _shutdown(self):
        self._stopping = True
        # Stop audio immediately, but keep the reader alive for final usage.
        for task in self._tasks[1:]:
            task.cancel()
        self._input.clear()
        if self._speaking:
            self._speaking = False
            try:
                await self.send_control("stop")
            except Exception:
                pass
        if self._socket:
            try:
                await self._send({"type": "session.close"})
                await asyncio.wait_for(self._finalized.wait(), self.config.close_timeout)
            except (Exception, asyncio.CancelledError):
                pass
        for task in self._tasks:
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        if self._socket:
            await self._socket.close()

    def _fail(self, error):
        if self._error is None:
            self._error = error
            self._error_event.set()
            self._ready.set()

    async def _read(self):
        try:
            while True:
                event = json.loads(await self._socket.recv())
                kind = event.get("type")
                if kind == "session.closed":
                    self.usage_seconds = float(event.get("usage", {}).get("seconds", self.usage_seconds))
                    self._finalized.set()
                    if not self._stopping:
                        self._fail(RuntimeError("Live session ended"))
                    return
                if kind == "session.started":
                    self.session_id = event["session"]["id"]
                    self._ready.set()
                elif kind == "error":
                    # Never put provider messages or authentication headers in logs.
                    self._fail(RuntimeError("Live provider error: " + event.get("error", {}).get("code", "unknown")))
                elif self._stopping or self._error:
                    continue
                elif kind == "session.output_audio.delta":
                    try:
                        pcm = base64.b64decode(event["delta"], validate=True)
                        if len(pcm) % 2 or len(pcm) > self.config.output_buffer_ms * 32:
                            raise ValueError("Invalid Live audio delta")
                        self._gate_audio(pcm)
                    except (ValueError, binascii.Error, asyncio.QueueFull):
                        self._fail(ValueError("Invalid or overflowing Live audio"))
                elif kind in ("session.input_transcript.delta", "session.output_transcript.delta"):
                    if kind == "session.input_transcript.delta" and event.get("delta", "").strip():
                        self._last_voice = time.monotonic()
                    if self.on_transcript:
                        await self.on_transcript("user" if "input_" in kind else "assistant", event)
                elif kind == "session.delegation.created" and self.on_delegation:
                    await self.on_delegation(event)
                elif kind == "session.usage":
                    self.usage_seconds = float(event.get("usage", {}).get("seconds", self.usage_seconds))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._fail(exc)

    def _gate_audio(self, pcm):
        self._output_partial.extend(pcm)
        while len(self._output_partial) >= self.INPUT_FRAME_BYTES:
            frame = bytes(self._output_partial[:self.INPUT_FRAME_BYTES])
            del self._output_partial[:self.INPUT_FRAME_BYTES]
            peak = np.max(np.abs(np.frombuffer(frame, dtype="<i2").astype(np.int32)))
            if peak > self.config.speech_threshold:
                self._last_voice = time.monotonic()
                self._quiet_ms = 0
                if not self._gate_active:
                    self._output.put_nowait(("start", None))
                    for previous in self._pre_roll:
                        self._output.put_nowait(("pcm", previous))
                    self._pre_roll.clear()
                    self._gate_active = True
                self._output.put_nowait(("pcm", frame))
            elif self._gate_active:
                self._output.put_nowait(("pcm", frame))
                self._quiet_ms += 20
                if self._quiet_ms >= self.config.silence_ms:
                    self._output.put_nowait(("stop", None))
                    self._gate_active = False
                    self._quiet_ms = 0
            else:
                self._pre_roll.append(frame)

    async def _pump_input(self):
        deadline = time.monotonic()
        try:
            while not self._stopping and self._error is None:
                if self._listening and not self._speaking and len(self._input) >= self.INPUT_FRAME_BYTES:
                    pcm = bytes(self._input[:self.INPUT_FRAME_BYTES])
                    del self._input[:self.INPUT_FRAME_BYTES]
                else:
                    pcm = bytes(self.INPUT_FRAME_BYTES)
                await self._send({"type": "session.input_audio.append", "audio": base64.b64encode(pcm).decode("ascii")})
                if time.monotonic() - self._last_voice > self.config.idle_timeout:
                    if self.on_idle:
                        asyncio.create_task(self.on_idle())
                    return
                deadline = max(deadline + 0.02, time.monotonic())
                await asyncio.sleep(max(0, deadline - time.monotonic()))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._fail(exc)

    async def _play_output(self):
        pcm = bytearray()
        deadline = time.monotonic()

        async def packet(data):
            nonlocal deadline
            await self.send_audio(self._encoder.encode(data, 960))
            deadline = max(deadline + 0.06, time.monotonic())
            await asyncio.sleep(max(0, deadline - time.monotonic()))

        try:
            while not self._stopping and self._error is None:
                kind, frame = await self._output.get()
                if kind == "start":
                    self._speaking = True
                    self._input.clear()
                    await self.send_control("start")
                    await asyncio.sleep(self.config.start_lead_ms / 1000)
                    deadline = time.monotonic()
                elif kind == "pcm":
                    pcm.extend(frame)
                    while len(pcm) >= self.OPUS_FRAME_BYTES:
                        await packet(bytes(pcm[:self.OPUS_FRAME_BYTES]))
                        del pcm[:self.OPUS_FRAME_BYTES]
                elif kind == "stop":
                    if pcm:
                        await packet(bytes(pcm) + bytes(self.OPUS_FRAME_BYTES - len(pcm)))
                        pcm.clear()
                    await asyncio.sleep(self.config.playback_tail_ms / 1000)
                    await self.send_control("stop")
                    self._speaking = False
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._fail(exc)
