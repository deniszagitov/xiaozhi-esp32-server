"""Client delegation to existing LLM, memory and UnifiedToolHandler providers."""

import asyncio
from collections import OrderedDict
import json
import time
import uuid

from core.utils.dialogue import Message


BACKEND_INSTRUCTIONS = (
    "Use current tool results for device state, never memory or assumptions. "
    "Do not invent device identifiers or exact entity names. For a state lookup, "
    "first discover the relevant devices with broad area/domain filters, then use "
    "the exact identifiers returned. If a guessed name gives an empty lookup, "
    "broaden a read-only discovery once before declaring the device absent. "
    "Never automatically retry an uncertain action. Return a brief, plain-language "
    "verified result in the user's language, without JSON or tool names."
)


def _value(obj, key, default=None):
    return obj.get(key, default) if isinstance(obj, dict) else getattr(obj, key, default)


def brief_result(text):
    """Bound UTF-8 bytes, conservatively below Live's 500-token append limit."""
    text = str(text).strip()
    if len(text.encode("utf-8")) <= 480:
        return text
    prefix = text.encode("utf-8")[:470].decode("utf-8", errors="ignore")
    return prefix.rsplit(" ", 1)[0] + "…"


class LiveBackend:
    def __init__(self, conn, *, timeout=45, memory_timeout=8, tool_timeout=20):
        self.conn = conn
        self.timeout = timeout
        self.memory_timeout = memory_timeout
        self.tool_timeout = tool_timeout
        self._lock = asyncio.Lock()
        self._results = OrderedDict()
        self._tool_results = OrderedDict()
        self._uncertain_calls = set()
        self._llm_task = None

    async def respond(self, query, delegation_id, *, is_current=None):
        is_current = is_current or (lambda: True)
        async with self._lock:
            if delegation_id in self._results:
                return self._results[delegation_id]
            if self._llm_task and not self._llm_task.done():
                return "Предыдущий запрос ещё выполняется. Результат пока неизвестен."
            result = await self._respond(query, is_current)
            self._results[delegation_id] = brief_result(result)
            while len(self._results) > 64:
                self._results.popitem(last=False)
            return self._results[delegation_id]

    async def _respond(self, query, is_current):
        if not is_current():
            return "Запрос прерван. Новые действия не выполнялись."
        if not query or len(query) > 8000:
            return "Не удалось получить полную фразу. Повтори, пожалуйста."
        conn = self.conn
        # The latest observed user turn may already have been added by captions.
        latest = next((message for message in reversed(conn.dialogue.dialogue)
                       if message.role == "user"), None)
        if latest is None or latest.content != query:
            conn.dialogue.put(Message(role="user", content=query))
        memory = ""
        if conn.memory:
            try:
                started = time.monotonic()
                lookup = getattr(conn.memory, "query_memory_checked", conn.memory.query_memory)
                memory = await asyncio.wait_for(lookup(query), self.memory_timeout)
                if getattr(conn, "logger", None):
                    conn.logger.info("GPT-Live memory lookup complete; duration_ms={:.0f}, context_chars={}",
                                     (time.monotonic() - started) * 1000, len(memory or ""))
            except Exception:
                return "Не удалось прочитать память. Действия с устройствами не выполнялись."
        if not is_current():
            return "Запрос прерван. Новые действия не выполнялись."
        handler = conn.func_handler
        if handler and not handler.finish_init:
            deadline = asyncio.get_running_loop().time() + 8
            while not handler.finish_init and asyncio.get_running_loop().time() < deadline:
                await asyncio.sleep(0.05)
            if not handler.finish_init:
                return "Инструменты ещё не готовы. Попробуй немного позже."
        functions = handler.get_functions() if handler else []
        tool_allowlist = conn.config.get("live", {}).get("allowed_tools")
        if tool_allowlist is not None:
            functions = [item for item in functions if item["function"]["name"] in tool_allowlist]
        allowed = {item["function"]["name"] for item in functions}
        for _ in range(4):
            if not is_current():
                return "Запрос прерван. Новые действия не выполнялись."
            dialogue = conn.dialogue.get_llm_dialogue_with_memory(memory, conn.config.get("voiceprint", {}))
            dialogue = [dict(message) for message in dialogue]
            if dialogue and dialogue[0].get("role") == "system":
                dialogue[0]["content"] = (dialogue[0].get("content") or "") + "\n\n" + BACKEND_INSTRUCTIONS
            else:
                dialogue.insert(0, {"role": "system", "content": BACKEND_INSTRUCTIONS})
            self._llm_task = asyncio.create_task(asyncio.to_thread(self._collect, dialogue, functions))
            try:
                text, calls = await asyncio.wait_for(asyncio.shield(self._llm_task), self.timeout)
                if getattr(conn, "logger", None):
                    conn.logger.info("GPT-Live backend returned; tool_calls={}", len(calls))
            except asyncio.TimeoutError:
                # The HTTP worker can still be alive; don't enqueue another one.
                return "Ответ сервера задерживается. Результат пока неизвестен."
            except Exception:
                return "Не удалось получить ответ сервера."
            if not is_current():
                return "Запрос прерван. Новые действия не выполнялись."
            if not calls:
                return text or "Сервер не вернул ответ."
            tool_calls = [{"id": call["id"], "type": "function", "function": {
                "name": call["name"], "arguments": call["arguments"]}} for call in calls]
            conn.dialogue.put(Message(role="assistant", content=text or None, tool_calls=tool_calls))
            direct_responses = []
            for index, call in enumerate(calls):
                signature = (call["name"], call["arguments"])
                if call["id"] in self._tool_results:
                    stored_signature, payload = self._tool_results[call["id"]]
                    if stored_signature != signature:
                        payload = self._payload("ERROR", "Tool call ID was reused with different arguments")
                    conn.dialogue.put(Message(role="tool", tool_call_id=call["id"], content=payload))
                    continue
                if not is_current():
                    payload = self._payload("NOTEXECUTED", "Request interrupted before tool dispatch")
                    self._tool_results[call["id"]] = (signature, payload)
                    while len(self._tool_results) > 64:
                        self._tool_results.popitem(last=False)
                    conn.dialogue.put(Message(role="tool", tool_call_id=call["id"], content=payload))
                    continue
                try:
                    arguments = json.loads(call["arguments"] or "{}")
                    if not isinstance(arguments, dict):
                        raise ValueError("Tool arguments must be an object")
                except (ValueError, TypeError):
                    status, content = "ERROR", "Invalid tool arguments"
                else:
                    tool_signature = (call["name"], json.dumps(arguments, sort_keys=True, separators=(",", ":")))
                    if tool_signature in self._uncertain_calls or len(self._uncertain_calls) >= 64:
                        status, content = "UNKNOWN", "Previous result unknown; automatic retry blocked"
                    elif call["name"] not in allowed:
                        status, content = "NOTFOUND", "Unknown tool"
                    else:
                        try:
                            result = await asyncio.wait_for(handler.handle_llm_function_call(
                                conn, {"name": call["name"], "arguments": arguments}), self.tool_timeout)
                            status = result.action.name
                            content = result.result if result.result is not None else result.response
                            if isinstance(content, dict) and content.get("execution_status") == "unknown":
                                status = "UNKNOWN"
                                self._uncertain_calls.add(tool_signature)
                            if status == "RESPONSE":
                                direct_responses.append(result.response or str(content or "Выполнено."))
                        except (asyncio.TimeoutError, asyncio.CancelledError) as exc:
                            self._uncertain_calls.add(tool_signature)
                            payload = self._payload("UNKNOWN", "Tool interrupted; do not retry automatically")
                            self._tool_results[call["id"]] = (signature, payload)
                            conn.dialogue.put(Message(role="tool", tool_call_id=call["id"],
                                content=payload))
                            for remaining in calls[index + 1:]:
                                conn.dialogue.put(Message(role="tool", tool_call_id=remaining["id"],
                                    content=self._payload("NOTEXECUTED", "Previous tool result is unknown")))
                            if isinstance(exc, asyncio.CancelledError):
                                raise
                            return "Не удалось подтвердить результат действия. Я не буду повторять его автоматически."
                        except Exception:
                            status, content = "ERROR", "Tool execution failed"
                payload = self._payload(status, content)
                self._tool_results[call["id"]] = (signature, payload)
                while len(self._tool_results) > 64:
                    self._tool_results.popitem(last=False)
                conn.dialogue.put(Message(role="tool", tool_call_id=call["id"], content=payload))
            if direct_responses:
                return " ".join(direct_responses)
        return "Запрос требует слишком много действий. Уточни, пожалуйста."

    @staticmethod
    def _payload(status, content):
        payload = json.dumps({"status": status, "result": content}, ensure_ascii=False, default=str)
        if len(payload) > 32768:
            payload = json.dumps({"status": status, "result": str(content)[:24000], "truncated": True}, ensure_ascii=False)
        return payload

    def _collect(self, dialogue, functions):
        if functions:
            stream = self.conn.llm.response_with_functions(
                self.conn.session_id, dialogue, functions=functions)
        else:
            stream = self.conn.llm.response(self.conn.session_id, dialogue)
        text = []
        text_size = 0
        calls = {}
        try:
            for chunk in stream:
                if functions:
                    content, deltas = chunk
                else:
                    content, deltas = chunk, None
                if content:
                    text_size += len(content)
                    if text_size > 8000:
                        raise ValueError("Backend response is too large")
                    text.append(content)
                for delta in deltas or []:
                    index = _value(delta, "index", 0)
                    call = calls.setdefault(index, {"id": "call_" + uuid.uuid4().hex, "name": "", "arguments": ""})
                    if _value(delta, "id"):
                        call["id"] = _value(delta, "id")
                    function = _value(delta, "function", {})
                    call["name"] += _value(function, "name", "") or ""
                    call["arguments"] += _value(function, "arguments", "") or ""
                    if len(calls) > 8 or len(call["arguments"]) > 16000 or len(call["name"]) > 128:
                        raise ValueError("Backend tool call is too large")
        finally:
            close = getattr(stream, "close", None)
            if close:
                close()
        return "".join(text), list(calls.values())
