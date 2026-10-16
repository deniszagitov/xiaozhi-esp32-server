"""Stock protocol controls, opt-in selection and generation boundaries."""

import asyncio
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from core.providers.live.runtime import LiveRuntime, enabled_for_device, resolve_config
from core.utils.dialogue import Dialogue


class DeviceSocket:
    def __init__(self):
        self.messages = []
        self.closed = False

    async def send(self, payload):
        if self.closed:
            raise ConnectionError("Device disconnected")
        self.messages.append(json.loads(payload) if isinstance(payload, str) else payload)

    async def close(self, **kwargs):
        self.closed = True


class CloudSession:
    def __init__(self, config, **callbacks):
        self.__dict__.update(callbacks)
        self.audio = []
        self.results = []
        self.listening = True
        self.closed = False
        self.started = asyncio.Event()
        self.usage_seconds = 1.0

    async def start(self):
        self.started.set()

    def feed_pcm(self, pcm):
        self.audio.append(pcm)

    def set_listening(self, enabled):
        self.listening = enabled

    async def append_result(self, delegation_id, content):
        self.results.append((delegation_id, content))

    async def wait_error(self):
        await asyncio.Event().wait()

    async def close(self):
        self.closed = True


class LiveRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        sleep = patch("asyncio.sleep", asyncio.tasks.sleep)
        sleep.start()
        self.addCleanup(sleep.stop)
        self.sessions = []
        self.conn = SimpleNamespace(
            config={"live": {"api_key": "synthetic-key"}},
            websocket=DeviceSocket(), conn_from_mqtt_gateway=False,
            dialogue=Dialogue(), session_id="stock-device", memory=None,
            func_handler=None, close_after_chat=False,
            logger=SimpleNamespace(info=lambda *args: None, warning=lambda *args: None),
        )

        def factory(config, **callbacks):
            session = CloudSession(config, **callbacks)
            self.sessions.append(session)
            return session

        self.runtime = LiveRuntime(self.conn, session_factory=factory)
        with patch.dict("os.environ", {}, clear=True):
            self.runtime.configure()
        self.addAsyncCleanup(self.runtime.close)

    async def start(self, state="start"):
        await self.runtime.listen({"state": state})
        for _ in range(30):
            await asyncio.sleep(0.01)
            if self.sessions and self.sessions[-1].started.is_set():
                return self.sessions[-1]
        self.fail("Session did not start")

    def test_live_is_opt_in_and_can_target_one_device(self):
        self.assertFalse(enabled_for_device({}, "AA:BB"))
        self.assertFalse(enabled_for_device({"live": {"enabled": "true"}}, "AA:BB"))
        config = {"live": {"enabled": True, "device_ids": ["aa:bb"]}}
        self.assertTrue(enabled_for_device(config, "AA:BB"))
        self.assertFalse(enabled_for_device(config, "other"))

    def test_stock_hello_negotiates_16k_and_rejects_other_transports(self):
        hello = {"version": 1, "audio_params": {"format": "opus", "sample_rate": 16000, "channels": 1}}
        self.runtime.validate_hello(hello)
        self.assertEqual(self.conn.sample_rate, 16000)
        with self.assertRaises(ValueError):
            self.runtime.validate_hello({**hello, "version": 2})
        self.conn.conn_from_mqtt_gateway = True
        with self.assertRaises(ValueError):
            self.runtime.validate_hello(hello)

    async def test_wake_cache_is_discarded_and_listen_controls_capture(self):
        self.runtime.feed_pcm(b"\x01\x00" * 960)
        session = await self.start("detect")
        self.assertEqual(session.audio, [])
        self.assertEqual(session.results[0][0], None)
        self.runtime.feed_pcm(b"\x02\x00" * 960)
        self.assertEqual(session.audio, [b"\x02\x00" * 960])
        await self.runtime.listen({"state": "stop"})
        self.assertFalse(session.listening)
        self.runtime.feed_pcm(b"\x03\x00" * 960)
        self.assertEqual(len(session.audio), 1)

    async def test_abort_drops_old_audio_and_reopens_a_fresh_generation(self):
        old = await self.start()
        await old.send_control("start")
        await old.send_audio(b"old-first")
        await self.runtime.abort()
        await old.send_audio(b"stale")
        await old.send_control("start")
        fresh = await self.start()
        await fresh.send_control("start")
        await fresh.send_audio(b"fresh")
        self.assertTrue(old.closed)
        self.assertEqual([item for item in self.conn.websocket.messages if isinstance(item, bytes)],
                         [b"old-first", b"fresh"])
        states = [item["state"] for item in self.conn.websocket.messages if isinstance(item, dict)]
        self.assertEqual(states, ["start", "stop", "start"])

    async def test_delegation_does_not_block_audio_and_abort_suppresses_late_reply(self):
        entered = asyncio.Event()
        release = asyncio.Event()

        async def memory(query):
            entered.set()
            await release.wait()
            return "verified memory"

        self.conn.memory = SimpleNamespace(query_memory=memory)
        self.conn.llm = SimpleNamespace(response=lambda *args: iter(["Проверенный ответ."]))
        session = await self.start()
        await session.on_transcript("user", {"delta": "Какая температура?", "start_ms": 10, "end_ms": 900})
        await session.on_delegation({"delegation": {"id": "del_test", "target": "client"}})
        await asyncio.wait_for(entered.wait(), 1)
        await session.send_audio(b"while-backend-waits")
        self.assertIn(b"while-backend-waits", self.conn.websocket.messages)
        await self.runtime.abort()
        release.set()
        await asyncio.sleep(0.05)
        self.assertEqual(session.results, [])

    async def test_device_disconnect_still_closes_cloud(self):
        session = await self.start()
        await self.conn.websocket.close()
        await self.runtime.close()
        self.assertTrue(session.closed)

    async def test_abort_during_memory_prevents_unsent_backend_actions(self):
        entered, release = asyncio.Event(), asyncio.Event()

        async def memory(query):
            entered.set()
            await release.wait()
            return "verified memory"

        llm_calls = []

        class BackendLLM:
            def response_with_functions(self, *args, **kwargs):
                llm_calls.append(True)
                yield None, [SimpleNamespace(index=0, id="unsent_call", function=SimpleNamespace(
                    name="mcp_switch", arguments='{}'))]

        execute = AsyncMock(return_value=SimpleNamespace(
            action=SimpleNamespace(name="RESPONSE"), result=None, response="Выполнено."))
        self.conn.memory = SimpleNamespace(query_memory=memory)
        self.conn.llm = BackendLLM()
        self.conn.func_handler = SimpleNamespace(finish_init=True,
            get_functions=lambda: [{"function": {"name": "mcp_switch"}}],
            handle_llm_function_call=execute)
        session = await self.start()
        await session.on_transcript("user", {"delta": "Включи устройство.", "start_ms": 10, "end_ms": 900})
        await session.on_delegation({"delegation": {"id": "del_abort_memory", "target": "client"}})
        await asyncio.wait_for(entered.wait(), 1)
        await self.runtime.abort()
        release.set()
        await asyncio.gather(self.runtime._worker, return_exceptions=True)
        self.assertEqual(llm_calls, [])
        execute.assert_not_awaited()
        self.assertEqual(session.results, [])

    async def test_abort_records_sent_action_as_unknown_and_skips_remaining_actions(self):
        entered, release = asyncio.Event(), asyncio.Event()
        dispatches = []

        async def execute(conn, call):
            dispatches.append(call["name"])
            entered.set()
            await release.wait()
            return SimpleNamespace(action=SimpleNamespace(name="REQLLM"), result="confirmed", response=None)

        class BackendLLM:
            def response_with_functions(self, *args, **kwargs):
                yield None, [SimpleNamespace(index=index, id=f"action_{index}", function=SimpleNamespace(
                    name=name, arguments='{}')) for index, name in enumerate(("mcp_first", "mcp_second"))]

        self.conn.llm = BackendLLM()
        self.conn.func_handler = SimpleNamespace(finish_init=True,
            get_functions=lambda: [{"function": {"name": name}} for name in ("mcp_first", "mcp_second")],
            handle_llm_function_call=execute)
        session = await self.start()
        await session.on_transcript("user", {"delta": "Выполни два действия.", "start_ms": 10, "end_ms": 900})
        await session.on_delegation({"delegation": {"id": "del_abort_actions", "target": "client"}})
        await asyncio.wait_for(entered.wait(), 1)
        await self.runtime.abort()
        release.set()
        await asyncio.gather(self.runtime._worker, return_exceptions=True)
        self.assertEqual(dispatches, ["mcp_first"])
        results = {message.tool_call_id: json.loads(message.content) for message in self.conn.dialogue.dialogue
                   if message.role == "tool"}
        self.assertEqual(results["action_0"]["status"], "UNKNOWN")
        self.assertEqual(results["action_1"]["status"], "NOTEXECUTED")
        self.assertEqual(session.results, [])

    async def test_abort_cancels_handler_wait_before_transport_dispatch(self):
        entered, release = asyncio.Event(), asyncio.Event()
        dispatches = []

        async def execute(conn, call):
            entered.set()
            await release.wait()  # Display delivery, readiness or another pre-send wait.
            dispatches.append(call["name"])
            return SimpleNamespace(action=SimpleNamespace(name="RESPONSE"), result=None, response="Выполнено.")

        class BackendLLM:
            def response_with_functions(self, *args, **kwargs):
                yield None, [SimpleNamespace(index=0, id="presend_call", function=SimpleNamespace(
                    name="mcp_switch", arguments='{}'))]

        self.conn.llm = BackendLLM()
        self.conn.func_handler = SimpleNamespace(finish_init=True,
            get_functions=lambda: [{"function": {"name": "mcp_switch"}}],
            handle_llm_function_call=execute)
        session = await self.start()
        await session.on_transcript("user", {"delta": "Включи устройство.", "start_ms": 10, "end_ms": 900})
        await session.on_delegation({"delegation": {"id": "del_presend", "target": "client"}})
        await asyncio.wait_for(entered.wait(), 1)
        await self.runtime.abort()
        release.set()
        await asyncio.gather(self.runtime._worker, return_exceptions=True)
        self.assertEqual(dispatches, [])
        self.assertEqual(session.results, [])

    async def test_new_user_turn_cancels_exit_waiting_on_backend(self):
        entered, release = asyncio.Event(), asyncio.Event()

        async def memory(query):
            entered.set()
            await release.wait()
            return "verified memory"

        async def exit_tool(conn, call):
            conn.close_after_chat = True
            return SimpleNamespace(action=SimpleNamespace(name="RESPONSE"), result=None, response="До свидания.")

        class BackendLLM:
            def response_with_functions(self, *args, **kwargs):
                yield None, [SimpleNamespace(index=0, id="delayed_exit", function=SimpleNamespace(
                    name="handle_exit_intent", arguments='{}'))]

        self.conn.memory = SimpleNamespace(query_memory=memory)
        self.conn.llm = BackendLLM()
        self.conn.func_handler = SimpleNamespace(finish_init=True,
            get_functions=lambda: [{"function": {"name": "handle_exit_intent"}}],
            handle_llm_function_call=exit_tool)
        session = await self.start()
        await session.on_transcript("user", {"delta": "Пока.", "start_ms": 10, "end_ms": 900})
        await session.on_delegation({"delegation": {"id": "del_delayed_exit", "target": "client"}})
        await asyncio.wait_for(entered.wait(), 1)
        await session.on_transcript("user", {"delta": "Подожди, какой сегодня день?", "start_ms": 3000, "end_ms": 4000})
        release.set()
        await asyncio.wait_for(self.runtime._worker, 1)
        self.assertFalse(self.conn.close_after_chat)
        self.assertIsNone(self.runtime._farewell_task)
        self.assertFalse(self.conn.websocket.closed)
        self.assertEqual(session.results[0][0], "del_delayed_exit")
        self.assertNotIn("До свидания", session.results[0][1])

    async def test_trailing_delta_of_same_user_turn_keeps_pending_exit(self):
        entered, release = asyncio.Event(), asyncio.Event()

        async def memory(query):
            entered.set()
            await release.wait()
            return "verified memory"

        async def exit_tool(conn, call):
            conn.close_after_chat = True
            return SimpleNamespace(action=SimpleNamespace(name="RESPONSE"), result=None, response="До свидания.")

        class BackendLLM:
            def response_with_functions(self, *args, **kwargs):
                yield None, [SimpleNamespace(index=0, id="trailing_exit", function=SimpleNamespace(
                    name="handle_exit_intent", arguments='{}'))]

        self.conn.memory = SimpleNamespace(query_memory=memory)
        self.conn.llm = BackendLLM()
        self.conn.func_handler = SimpleNamespace(finish_init=True,
            get_functions=lambda: [{"function": {"name": "handle_exit_intent"}}],
            handle_llm_function_call=exit_tool)
        session = await self.start()
        await session.on_transcript("user", {"delta": "Пока", "start_ms": 10, "end_ms": 900})
        await session.on_delegation({"delegation": {"id": "del_trailing_exit", "target": "client"}})
        await asyncio.wait_for(entered.wait(), 1)
        await session.on_transcript("user", {"delta": ".", "start_ms": 900, "end_ms": 950})
        release.set()
        await asyncio.wait_for(self.runtime._worker, 1)
        self.assertTrue(self.conn.close_after_chat)
        self.assertIsNotNone(self.runtime._farewell_task)
        self.assertEqual(session.results, [("del_trailing_exit", "До свидания.")])
        # More trailing text must not cancel the already scheduled farewell either.
        await session.on_transcript("user", {"delta": " ", "start_ms": 950, "end_ms": 970})
        self.assertTrue(self.conn.close_after_chat)
        self.assertFalse(self.runtime._farewell_task.cancelled())

    async def test_exit_waits_for_farewell_after_backend_preamble(self):
        async def exit_tool(conn, call):
            conn.close_after_chat = True
            return SimpleNamespace(action=SimpleNamespace(name="RESPONSE"), result=None, response="До свидания.")

        class BackendLLM:
            def response_with_functions(self, *args, **kwargs):
                yield None, [SimpleNamespace(index=0, id="exit_call", function=SimpleNamespace(
                    name="handle_exit_intent", arguments='{}'))]

        self.conn.func_handler = SimpleNamespace(finish_init=True,
            get_functions=lambda: [{"type": "function", "function": {"name": "handle_exit_intent"}}],
            handle_llm_function_call=exit_tool)
        self.conn.llm = BackendLLM()
        session = await self.start()
        await session.on_transcript("user", {"delta": "Пока.", "start_ms": 10, "end_ms": 900})
        await session.send_control("start")  # "Let me respond" preamble.
        await session.on_delegation({"delegation": {"id": "del_exit", "target": "client"}})
        for _ in range(100):
            if session.results:
                break
            await asyncio.sleep(0.01)
        self.assertEqual(session.results[0][1], "До свидания.")
        await session.send_control("stop")
        await asyncio.sleep(0.01)
        self.assertFalse(self.conn.websocket.closed)
        await session.send_control("start")
        await session.send_audio(b"farewell")
        await session.send_control("stop")
        for _ in range(250):
            if self.conn.websocket.closed:
                break
            await asyncio.sleep(0.01)
        self.assertTrue(self.conn.websocket.closed)
