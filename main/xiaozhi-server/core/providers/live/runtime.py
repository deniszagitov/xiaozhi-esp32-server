"""Opt-in GPT-Live lifecycle for the existing Xiaozhi WebSocket connection."""

import asyncio
from collections import deque
import json
import os
import time

from core.providers.live.delegation import LiveBackend, brief_result
from core.providers.live.openai_live import LiveConfig, LiveSession
from core.utils.dialogue import Message


def enabled_for_device(config, device_id):
    settings = config.get("live", {})
    if settings.get("enabled") is not True:
        return False
    devices = settings.get("device_ids", [])
    return not devices or str(device_id).lower() in {str(value).lower() for value in devices}


def resolve_config(config):
    settings = config.get("live", {})
    key = os.environ.get(settings.get("api_key_env", "OPENAI_API_KEY"), "")
    if not key:
        key = settings.get("api_key", "")
    if not key:
        selected = config.get("selected_module", {}).get("LLM")
        key = config.get("LLM", {}).get(selected, {}).get("api_key", "")
    fields = {name: settings[name] for name in LiveConfig.__dataclass_fields__
              if name not in ("api_key", "url") and name in settings}
    if "instructions" in fields:
        fields["instructions"] = LiveConfig.__dataclass_fields__["instructions"].default + "\n" + fields["instructions"]
    return LiveConfig(api_key=key, **fields)


class LiveRuntime:
    def __init__(self, conn, *, session_factory=LiveSession):
        self.conn = conn
        self.session_factory = session_factory
        self.backend = LiveBackend(conn)
        self._ready = asyncio.Event()
        self._config = None
        self._session = None
        self._generation = 0
        self._start_task = None
        self._closing_tasks = set()
        self._error_task = None
        self._delegations = asyncio.Queue(maxsize=2)
        self._worker = None
        self._playbacks = 0
        self._farewell_task = None
        self._pending = deque()
        self._pending_size = 0
        self._closed = False
        self._listening = True
        self._streaming = False
        self._activated = False
        self._caption = {}
        self._last_transcript = 0.0
        self._user_revision = 0
        self._seen_delegations = deque(maxlen=64)

    def configure(self):
        self._config = resolve_config(self.conn.config)
        self._ready.set()

    def validate_hello(self, message):
        params = message.get("audio_params", {})
        if (self.conn.conn_from_mqtt_gateway or message.get("version", 1) != 1
                or params.get("format") != "opus" or params.get("sample_rate") != 16000
                or params.get("channels", 1) != 1 or params.get("frame_duration", 60) != 60):
            raise ValueError("GPT-Live requires WebSocket v1, mono Opus 16 kHz / 60 ms")
        self.conn.sample_rate = 16000

    async def listen(self, message):
        state = message.get("state")
        if state == "detect":
            self._activated = True
            # Firmware sends cached wake-word audio before detect; don't replay it
            # into a new cloud session or accumulate two seconds of input latency.
            self._pending.clear()
            self._pending_size = 0
            self._listening = True
            self._request_start(wake=True)
        elif state == "start":
            self._activated = True
            self._listening = True
            if self._session:
                self._session.set_listening(True)
            self._request_start()
        elif state == "stop":
            self._activated = False
            self._listening = False
            if self._session:
                self._session.set_listening(False)

    def feed_pcm(self, pcm):
        if self._closed or not self._listening or not self._activated:
            return
        self.conn.last_activity_time = time.time() * 1000
        if self._session and self._streaming:
            self._session.feed_pcm(pcm)
        else:
            # Never silently truncate a command while the cloud is connecting.
            limit = (self._config.input_buffer_ms if self._config else 6000) * 32
            if len(pcm) % 2:
                raise ValueError("Invalid microphone PCM")
            if self._pending_size + len(pcm) > limit:
                raise BufferError("Live startup microphone buffer is full")
            self._pending.append(pcm)
            self._pending_size += len(pcm)

    def _request_start(self, wake=False):
        if self._closed or self._session or (self._start_task and not self._start_task.done()):
            return
        self._generation += 1
        self._start_task = asyncio.create_task(self._start(self._generation, wake), name="live-start")

    async def _start(self, generation, wake):
        try:
            await asyncio.wait_for(self._ready.wait(), 30)
            if self._closing_tasks:
                await asyncio.gather(*tuple(self._closing_tasks), return_exceptions=True)
            if self._closed or generation != self._generation:
                return
            self._caption.clear()

            async def audio(packet):
                if self._current(generation):
                    await self.conn.websocket.send(packet)

            async def control(state):
                if self._current(generation):
                    self.conn.client_is_speaking = state == "start"
                    if state == "start":
                        self._playbacks += 1
                    await self._device_message("tts", state=state)

            async def transcript(role, event):
                if self._current(generation):
                    await self._transcript(role, event)

            async def delegation(event):
                if self._current(generation):
                    self._delegate(event, generation)

            async def idle():
                if self._current(generation):
                    await self.conn.websocket.close()

            session = self.session_factory(self._config, send_audio=audio, send_control=control,
                on_transcript=transcript, on_delegation=delegation, on_idle=idle)
            self._session = session
            session.set_listening(self._listening)
            await session.start()
            if not self._current(generation):
                return
            while self._pending:
                session.feed_pcm(self._pending.popleft())
            self._pending_size = 0
            self._streaming = True
            self.conn.logger.info("GPT-Live ready: mono PCM/Opus 16 kHz")
            self._error_task = asyncio.create_task(self._watch_error(session, generation))
            if wake:
                await session.append_result(None, "The user activated the assistant. Briefly acknowledge that you are listening, in the configured conversation language.")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if self._current(generation):
                self.conn.logger.warning("GPT-Live startup failed ({})", type(exc).__name__)
                await self.conn.websocket.close(code=1011, reason="GPT-Live unavailable")

    def _current(self, generation):
        return not self._closed and generation == self._generation

    async def _watch_error(self, session, generation):
        error = await session.wait_error()
        if self._current(generation):
            self.conn.logger.warning("GPT-Live session failed ({})", type(error).__name__)
            await self.conn.websocket.close(code=1011, reason="GPT-Live unavailable")

    async def _device_message(self, kind, **fields):
        await self.conn.websocket.send(json.dumps({"type": kind, "session_id": self.conn.session_id,
                                                   **fields}, ensure_ascii=False))

    async def _transcript(self, role, event):
        delta = event.get("delta", "")
        if not isinstance(delta, str) or len(delta) > 8000:
            raise ValueError("Invalid Live transcript")
        now = time.monotonic()
        previous = self._caption.get(role)
        # Primary transcripts have no final-turn event. Timestamp gaps provide
        # caption/history grouping only; they never commit or trigger audio.
        start_ms = event.get("start_ms", 0)
        end_ms = event.get("end_ms", start_ms)
        new_message = previous is None or start_ms - previous[1] > 1200
        if new_message:
            message = Message(role=role, content=delta)
            self.conn.dialogue.put(message)
        else:
            message = previous[0]
            message.content = (message.content + delta)[-8000:]
        self._caption[role] = (message, end_ms)
        if role == "user":
            self._last_transcript = now
            if new_message:
                self._user_revision += 1
                self.conn.close_after_chat = False
                if self._farewell_task and not self._farewell_task.done():
                    self._farewell_task.cancel()
        await self._device_message("stt" if role == "user" else "tts",
            **({"text": message.content} if role == "user" else {"state": "sentence_start", "text": message.content}))

    def _delegate(self, event, generation):
        delegation = event.get("delegation", {})
        delegation_id = delegation.get("id")
        if delegation.get("target") != "client" or not isinstance(delegation_id, str):
            return
        if delegation_id in self._seen_delegations:
            return
        self.conn.logger.info("GPT-Live client delegation received")
        self._seen_delegations.append(delegation_id)
        # Do not await backend tools in the primary WebSocket reader.
        self._delegations.put_nowait((delegation_id, generation, self._session))
        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(self._work(), name="live-delegation")
            self._worker.add_done_callback(self._worker_finished)

    def _worker_finished(self, task):
        if not task.cancelled() and task.exception() and not self._closed:
            self.conn.logger.warning("GPT-Live backend worker failed ({})", type(task.exception()).__name__)
            asyncio.create_task(self.conn.websocket.close(code=1011, reason="GPT-Live backend unavailable"))

    async def _work(self):
        while not self._closed and not self._delegations.empty():
            delegation_id, generation, session = await self._delegations.get()
            # Delegation may precede the final transcript delta. Settle briefly,
            # but keep audio flowing and cap the wait at 1.5 seconds.
            deadline = time.monotonic() + 1.5
            while time.monotonic() < deadline:
                await asyncio.sleep(0.1)
                if "user" in self._caption and time.monotonic() - self._last_transcript >= 0.3:
                    break
            if not self._current(generation):
                continue
            user = self._caption.get("user")
            query = user[0].content if user else ""
            user_revision = self._user_revision
            result = await self.backend.respond(query, delegation_id,
                is_current=lambda: self._current(generation))
            if self._current(generation):
                if self.conn.close_after_chat and user_revision != self._user_revision:
                    self.conn.close_after_chat = False
                    result = "The user continued the conversation. Keep listening; do not say goodbye or end the session."
                playback_boundary = self._playbacks
                await session.append_result(delegation_id, brief_result(result))
                if self.conn.close_after_chat:
                    self._farewell_task = asyncio.create_task(self._close_after_farewell(generation, playback_boundary))
            else:
                # A started exit tool may finish after abort reset the close flag.
                self.conn.close_after_chat = False

    async def _close_after_farewell(self, generation, playback_boundary):
        started = time.monotonic()
        while self._current(generation) and self.conn.close_after_chat:
            await asyncio.sleep(0.05)
            elapsed = time.monotonic() - started
            # A preamble may stop after backend completion. Wait for the later
            # speech segment, with a bounded fallback for merged/silent replies.
            if not self.conn.client_is_speaking and (
                (self._playbacks > playback_boundary and elapsed >= 2) or elapsed >= 12
            ):
                await self.conn.websocket.close()
                return
            if elapsed >= 30:
                await self.conn.websocket.close()
                return

    async def abort(self):
        self._generation += 1  # Invalidate every old audio/caption callback first.
        self.conn.close_after_chat = False
        self.conn.client_is_speaking = False
        # A generation check before a handler cannot protect its later pre-send
        # awaits. Cancel through that call chain before accepting another turn.
        if self._worker and not self._worker.done():
            self._worker.cancel()
            await asyncio.gather(self._worker, return_exceptions=True)
        while not self._delegations.empty():
            self._delegations.get_nowait()
        self.conn.close_after_chat = False
        if self._start_task and not self._start_task.done():
            task, self._start_task = self._start_task, None
            task.cancel()
            self._closing_tasks.add(task)
            task.add_done_callback(self._closing_tasks.discard)
        if self._error_task:
            self._error_task.cancel()
        if self._farewell_task:
            self._farewell_task.cancel()
        session, self._session = self._session, None
        self._streaming = False
        self._pending.clear()
        self._pending_size = 0
        try:
            await self._device_message("tts", state="stop")
        except Exception:
            pass  # Device may already have disconnected; still close the cloud.
        if session:
            task = asyncio.create_task(self._close_session(session))
            self._closing_tasks.add(task)
            task.add_done_callback(self._closing_tasks.discard)
        # A cancelled in-flight tool is recorded as UNKNOWN, with no automatic
        # retry. Interruption cannot undo a command already delivered.

    async def _close_session(self, session):
        await session.close()
        self.conn.logger.info("GPT-Live closed; usage_seconds={:.2f}", session.usage_seconds)

    async def close(self):
        if self._closed:
            return
        await self.abort()
        self._closed = True
        if self._worker and not self._worker.done():
            try:
                await asyncio.wait_for(asyncio.shield(self._worker), 2)
            except asyncio.TimeoutError:
                self._worker.cancel()
            await asyncio.gather(self._worker, return_exceptions=True)
        if self._closing_tasks:
            await asyncio.gather(*tuple(self._closing_tasks), return_exceptions=True)
        if self._start_task:
            await asyncio.gather(self._start_task, return_exceptions=True)
