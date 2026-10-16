"""Client delegation reuses memory and the existing tool execution interface."""

import asyncio
import json
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from core.providers.live.delegation import LiveBackend
from core.utils.dialogue import Dialogue, Message


def mcp_adapters():
    # Adapter imports initialize module loggers; these tests need no server files.
    with patch("config.logger.setup_logging"):
        from core.providers.tools.device_mcp.mcp_executor import DeviceMCPExecutor
        from core.providers.tools.mcp_endpoint.mcp_endpoint_executor import MCPEndpointExecutor
        from core.providers.tools.server_mcp.mcp_executor import ServerMCPExecutor
    return (
        (DeviceMCPExecutor, "core.providers.tools.device_mcp.mcp_executor.call_mcp_tool"),
        (MCPEndpointExecutor, "core.providers.tools.mcp_endpoint.mcp_endpoint_executor.call_mcp_endpoint_tool"),
        (ServerMCPExecutor, None),
    )


class LiveDelegationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        sleep = patch("asyncio.sleep", asyncio.tasks.sleep)
        sleep.start()
        self.addCleanup(sleep.stop)
        self.order = []
        self.dialogue = Dialogue()
        self.dialogue.put(Message(role="system", content="Ответь по-русски. <memory></memory>"))

        async def memory(query):
            self.order.append("memory")
            return "Пользователь спрашивает про гостиную."

        async def execute(conn, call):
            self.order.append("tool")
            self.assertEqual(call["name"], "mcp_temperature")
            self.assertEqual(call["arguments"], {"room": "living"})
            return SimpleNamespace(action=SimpleNamespace(name="REQLLM"), result='{"temperature": 23}', response=None)

        self.conn = SimpleNamespace(
            dialogue=self.dialogue, session_id="device_test", config={},
            memory=SimpleNamespace(query_memory=memory),
            func_handler=SimpleNamespace(
                finish_init=True,
                get_functions=lambda: [{"type": "function", "function": {
                    "name": "mcp_temperature", "parameters": {"type": "object"}}}],
                handle_llm_function_call=execute,
            ),
        )

    async def test_memory_precedes_backend_and_verified_tool_result_is_returned(self):
        owner = self

        class BackendLLM:
            def response_with_functions(self, session_id, dialogue, functions):
                owner.order.append("llm")
                if not any(message["role"] == "tool" for message in dialogue):
                    yield None, [SimpleNamespace(index=0, id="call_1", function=SimpleNamespace(
                        name="mcp_temperature", arguments='{"room":"living"}'))]
                else:
                    self.result_dialogue = dialogue
                    yield "В гостиной 23 градуса.", None

        self.conn.llm = BackendLLM()
        backend = LiveBackend(self.conn)
        result = await backend.respond("Какая температура в гостиной?", "del_1")
        self.assertEqual(result, "В гостиной 23 градуса.")
        self.assertEqual(self.order, ["memory", "llm", "tool", "llm"])
        tool = next(message for message in self.conn.llm.result_dialogue if message["role"] == "tool")
        self.assertEqual(json.loads(json.loads(tool["content"])["result"])["temperature"], 23)
        cached = await backend.respond("Какая температура в гостиной?", "del_1")
        self.assertEqual(cached, result)
        self.assertEqual(self.order.count("tool"), 1)

    async def test_unavailable_memory_prevents_backend_and_tool_execution(self):
        async def unavailable(query):
            raise RuntimeError("Memory unavailable")

        self.conn.memory.query_memory_checked = unavailable
        self.conn.llm = SimpleNamespace(response_with_functions=lambda *args, **kwargs: self.fail("Backend must not run"))
        result = await LiveBackend(self.conn).respond("Температура?", "del_memory_failure")
        self.assertIn("Не удалось прочитать память", result)
        self.assertEqual(self.order, [])

    async def test_tool_timeout_is_unknown_and_equivalent_call_is_not_retried(self):
        attempts = []

        async def slow_tool(conn, call):
            attempts.append(call)
            await asyncio.sleep(10)

        class BackendLLM:
            def response_with_functions(self, session_id, dialogue, functions):
                if any('"status": "UNKNOWN"' in message.get("content", "") for message in dialogue if message["role"] == "tool"):
                    yield "Результат действия неизвестен.", None
                else:
                    yield None, [SimpleNamespace(index=0, id="slow_call", function=SimpleNamespace(
                        name="mcp_temperature", arguments='{"room":"living"}'))]

        self.conn.func_handler.handle_llm_function_call = slow_tool
        self.conn.llm = BackendLLM()
        backend = LiveBackend(self.conn, tool_timeout=0.01)
        result = await backend.respond("Температура?", "del_slow")
        self.assertIn("Не удалось подтвердить", result)
        tool = next(message for message in self.dialogue.dialogue if message.role == "tool")
        self.assertEqual(json.loads(tool.content)["status"], "UNKNOWN")
        self.assertEqual(len(attempts), 1)

        class RetryLLM:
            count = 0

            def response_with_functions(self, session_id, dialogue, functions):
                self.count += 1
                if self.count == 1:
                    yield None, [SimpleNamespace(index=0, id="new_call_id", function=SimpleNamespace(
                        name="mcp_temperature", arguments=' { "room" : "living" } '))]
                else:
                    yield "Результат пока неизвестен.", None

        self.conn.llm = RetryLLM()
        await backend.respond("Проверь ещё раз.", "del_retry")
        self.assertEqual(len(attempts), 1)

    async def test_tool_failure_is_given_to_backend_as_failure(self):
        async def execute(conn, call):
            return SimpleNamespace(action=SimpleNamespace(name="ERROR"), result=None, response="Home Assistant unavailable")

        self.conn.func_handler.handle_llm_function_call = execute
        captured = []

        class BackendLLM:
            def response_with_functions(self, session_id, dialogue, functions):
                if not any(message["role"] == "tool" for message in dialogue):
                    yield None, [SimpleNamespace(index=0, id="call_bad", function=SimpleNamespace(
                        name="mcp_temperature", arguments='{}'))]
                else:
                    captured.extend(dialogue)
                    yield "Не удалось прочитать температуру.", None

        self.conn.llm = BackendLLM()
        result = await LiveBackend(self.conn).respond("Температура?", "del_bad")
        self.assertEqual(result, "Не удалось прочитать температуру.")
        tool = next(message for message in captured if message["role"] == "tool")
        self.assertIn('"status": "ERROR"', tool["content"])

    async def test_split_tool_arguments_are_combined_without_duplicate_execution(self):
        owner = self

        class BackendLLM:
            def response_with_functions(self, session_id, dialogue, functions):
                if not any(message["role"] == "tool" for message in dialogue):
                    yield None, [SimpleNamespace(index=0, id="call_split", function=SimpleNamespace(
                        name="mcp_temperature", arguments='{"room":'))]
                    yield None, [SimpleNamespace(index=0, id=None, function=SimpleNamespace(
                        name=None, arguments='"living"}'))]
                else:
                    yield "23 градуса.", None

        self.conn.llm = BackendLLM()
        await LiveBackend(self.conn).respond("Температура?", "del_split")
        self.assertEqual(self.order.count("tool"), 1)

    async def test_mcp_transport_failure_blocks_equivalent_call_with_new_id(self):
        for executor_type, transport_path in mcp_adapters():
            for error_type in (ConnectionError, ValueError):
                with self.subTest(adapter=executor_type.__name__, error=error_type.__name__):
                    self.conn.live = object()
                    self.conn.dialogue = Dialogue()
                    self.conn.dialogue.put(Message(role="system", content="Use verified results."))
                    client = SimpleNamespace(is_ready=AsyncMock(return_value=True))
                    self.conn.mcp_client = self.conn.mcp_endpoint_client = client
                    transport = AsyncMock(side_effect=error_type("synthetic post-dispatch failure"))
                    executor = executor_type(self.conn)
                    if transport_path is None:
                        executor._initialized = True
                        executor.mcp_manager = SimpleNamespace(execute_tool=transport)

                    async def execute(conn, call):
                        return await executor.execute(conn, call["name"], call["arguments"])

                    class RetryLLM:
                        count = 0

                        def response_with_functions(self, *args, **kwargs):
                            self.count += 1
                            if self.count <= 2:
                                arguments = '{"room":"living"}' if self.count == 1 else ' { "room": "living" } '
                                yield None, [SimpleNamespace(index=0, id=f"call_{self.count}", function=SimpleNamespace(
                                    name="mcp_temperature", arguments=arguments))]
                            else:
                                yield "Результат действия неизвестен.", None

                    self.conn.func_handler.handle_llm_function_call = execute
                    self.conn.llm = RetryLLM()
                    backend = LiveBackend(self.conn)
                    if transport_path:
                        with patch(transport_path, transport):
                            await backend.respond("Включи устройство.", "del_transport")
                    else:
                        await backend.respond("Включи устройство.", "del_transport")
                    self.assertEqual(transport.await_count, 1)
                    results = [json.loads(message.content) for message in self.conn.dialogue.dialogue
                               if message.role == "tool"]
                    self.assertEqual([result["status"] for result in results], ["UNKNOWN", "UNKNOWN"])
                    self.assertNotIn("synthetic post-dispatch failure", json.dumps(results))

    async def test_legacy_mcp_transport_failure_policy_is_preserved(self):
        client = SimpleNamespace(is_ready=AsyncMock(return_value=True))
        self.conn.mcp_client = self.conn.mcp_endpoint_client = client
        for executor_type, transport_path in mcp_adapters():
            for error_type, expected_action in ((ConnectionError, "ERROR"), (ValueError, "NOTFOUND")):
                with self.subTest(adapter=executor_type.__name__, error=error_type.__name__):
                    executor = executor_type(self.conn)
                    transport = AsyncMock(side_effect=error_type("synthetic failure"))
                    if transport_path:
                        with patch(transport_path, transport):
                            result = await executor.execute(self.conn, "mcp_temperature", {})
                    else:
                        executor._initialized = True
                        executor.mcp_manager = SimpleNamespace(execute_tool=transport)
                        result = await executor.execute(self.conn, "mcp_temperature", {})
                    self.assertEqual(result.action.name, expected_action)
                    self.assertEqual(result.response, "synthetic failure")
                    self.assertIsNone(result.result)

    async def test_interrupted_llm_response_cannot_dispatch_a_new_action(self):
        entered, release = threading.Event(), threading.Event()
        active = [True]
        self.addCleanup(release.set)

        class BackendLLM:
            def response_with_functions(self, *args, **kwargs):
                entered.set()
                if not release.wait(2):
                    raise TimeoutError("Synthetic backend was not released")
                yield None, [SimpleNamespace(index=0, id="late_call", function=SimpleNamespace(
                    name="mcp_temperature", arguments='{}'))]

        execute = AsyncMock()
        self.conn.llm = BackendLLM()
        self.conn.func_handler.handle_llm_function_call = execute
        task = asyncio.create_task(LiveBackend(self.conn).respond(
            "Включи устройство.", "del_late_backend", is_current=lambda: active[0]))
        self.assertTrue(await asyncio.to_thread(entered.wait, 1))
        active[0] = False
        release.set()
        await asyncio.wait_for(task, 1)
        execute.assert_not_awaited()
