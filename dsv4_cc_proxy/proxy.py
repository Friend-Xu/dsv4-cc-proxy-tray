# dsv4-cc-proxy / proxy — 核心代理逻辑
#
# 环境变量:
#   PROXY_UPSTREAM    DeepSeek API 地址 (默认 https://api.deepseek.com/anthropic)
#   PROXY_HOST        监听地址 (默认 127.0.0.1)
#   PROXY_PORT        监听端口 (默认 16889)
#   PROXY_LOG_LEVEL   日志级别 (默认 warning)
#   PROXY_LOG_FILE    日志文件路径 (默认空=仅 stdout)
#   PROXY_LOG_MAX_BYTES  日志文件最大字节数 (默认 10MB)
#   PROXY_LOG_BACKUP_COUNT 轮转备份数量 (默认 3)
#   PROXY_DUMP_DIR    流量捕获目录 (默认空=关闭, ⚠ 含敏感数据)
#
# 参考: https://api-docs.deepseek.com/guides/thinking_mode

from __future__ import annotations

import json
import logging
import logging.handlers
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from starlette.applications import Starlette
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route

from dsv4_cc_proxy._version import VERSION

# ---- 配置 ----

DEEPSEEK_BASE = os.getenv("PROXY_UPSTREAM", "https://api.deepseek.com/anthropic")
DEEPSEEK_CHAT_BASE = os.getenv("PROXY_UPSTREAM_CHAT", "https://api.deepseek.com/v1")
HOST = os.getenv("PROXY_HOST", "127.0.0.1")
try:
    PORT = int(os.getenv("PROXY_PORT", "16889"))
except (TypeError, ValueError):
    print("Error: PROXY_PORT must be an integer", file=sys.stderr)
    sys.exit(1)
LOG_LEVEL = os.getenv("PROXY_LOG_LEVEL", "warning")
DUMP_DIR = os.getenv("PROXY_DUMP_DIR", "")

# SSE 流处理参数上限
MAX_EVENT_TYPES = 50
MAX_FILTERED_LINES = 200
DUMP_PREVIEW_LINES = 30
DUMP_MAX_BYTES = 500000
LOG_EVENT_PREVIEW = 15
LOG_FILE = os.getenv("PROXY_LOG_FILE", "")
LOG_MAX_BYTES = int(os.getenv("PROXY_LOG_MAX_BYTES", str(10 * 1024 * 1024)))
LOG_BACKUP_COUNT = int(os.getenv("PROXY_LOG_BACKUP_COUNT", "3"))

_log_format = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("deepseek-proxy")


def _setup_logging():
    level = getattr(logging, LOG_LEVEL.upper(), logging.WARNING)
    _root = logging.getLogger()
    _root.setLevel(level)
    _root.handlers.clear()

    _handler = logging.StreamHandler(sys.stdout)
    _handler.setFormatter(_log_format)
    _root.addHandler(_handler)

    if LOG_FILE:
        _fh = logging.handlers.RotatingFileHandler(
            LOG_FILE, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUP_COUNT, encoding="utf-8"
        )
        _fh.setFormatter(_log_format)
        _root.addHandler(_fh)


if not os.environ.get("PROXY_GUI_MODE"):
    _setup_logging()

_shared_client: httpx.AsyncClient | None = None


# ---- httpx 客户端 ----


def _get_client() -> httpx.AsyncClient:
    global _shared_client
    if _shared_client is None or _shared_client.is_closed:
        _shared_client = httpx.AsyncClient(
            timeout=httpx.Timeout(600.0, connect=10.0),
            limits=httpx.Limits(max_keepalive_connections=8, max_connections=20),
            trust_env=False,
        )
    return _shared_client


# ---- 修复 4: reasoning 记忆-回注 (多轮 agentic loop) ----

_reasoning_store: dict[str, str] = {}
_REASONING_STORE_LIMIT = 1000


def _tool_use_key(content: list) -> str | None:
    """用 tool_use id 列表排序后拼接做 key，因为这些 id 会跨轮次保持稳定。"""
    ids = sorted(b["id"] for b in content if isinstance(b, dict) and b.get("type") == "tool_use" and "id" in b)
    return "|".join(ids) if ids else None


# ---- 健康检查 ----


async def health(request):
    return JSONResponse(
        {
            "status": "ok",
            "version": VERSION,
            "upstream": DEEPSEEK_BASE,
        }
    )


# ---- 修复 1: 请求端 thinking 注入 ----


def _has_tool_use(content: list) -> bool:
    return any(isinstance(b, dict) and b.get("type") == "tool_use" for b in content)


def _has_thinking(content: list) -> bool:
    return any(isinstance(b, dict) and b.get("type") in ("thinking", "redacted_thinking") for b in content)


def _inject_thinking_blocks(data: dict) -> bool:
    thinking_cfg = data.get("thinking", {})
    if not isinstance(thinking_cfg, dict):
        return False
    if thinking_cfg.get("type") != "enabled":
        return False

    model = data.get("model", "")
    if not isinstance(model, str) or not model.lower().startswith("deepseek-v4"):
        return False

    modified = False
    for msg in data.get("messages", []):
        if msg.get("role") != "assistant":
            continue
        content = msg.get("content", [])
        if isinstance(content, str):
            continue
        if _has_tool_use(content) and not _has_thinking(content):
            key = _tool_use_key(content)
            stored = _reasoning_store.get(key, "") if key else ""
            thinking_text = stored if stored else ""
            for i, block in enumerate(content):
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    content.insert(i, {"type": "thinking", "thinking": thinking_text})
                    modified = True
                    if stored:
                        logger.info("[CC-REASONING] injected stored reasoning key=%s", key)
                    break
    return modified


# ---- 修复 2: thinking 模式标准化 ----


def _normalize_thinking(data: dict) -> bool:
    if "thinking" not in data:
        return False
    thinking_cfg = data["thinking"]
    if not isinstance(thinking_cfg, dict):
        return False

    thinking_type = thinking_cfg.get("type", "")
    if thinking_type in ("enabled", "disabled"):
        return False

    data["thinking"] = {"type": "disabled"}

    for key in ("reasoning_effort", "output_config"):
        val = data.pop(key, None)
        if val is not None:
            logger.info("[CC-THINKING] removed %s=%s", key, val)

    stripped = 0
    for msg in data.get("messages", []):
        if msg.get("role") != "assistant":
            continue
        content = msg.get("content", [])
        if isinstance(content, str):
            continue
        new_content = [
            b for b in content if not (isinstance(b, dict) and b.get("type") in ("thinking", "redacted_thinking"))
        ]
        if len(new_content) != len(content):
            stripped += len(content) - len(new_content)
            msg["content"] = new_content

    logger.info(
        "[CC-THINKING] converted %s → disabled, stripped %d thinking blocks",
        thinking_type,
        stripped,
    )
    return True


# ---- 修复 3: 响应端 thinking 剥离 ----


def _thinking_requested(data: dict) -> bool:
    thinking_cfg = data.get("thinking", {})
    return isinstance(thinking_cfg, dict) and thinking_cfg.get("type") == "enabled"


def _filter_sse_line(line: str, thinking_indices: set, thinking_buffers: dict | None = None) -> tuple:
    if not line.startswith("data: "):
        return line, thinking_indices

    try:
        data = json.loads(line[6:])
    except json.JSONDecodeError:
        return line, thinking_indices

    t = data.get("type", "")

    if t == "content_block_start":
        cb = data.get("content_block", {})
        if cb.get("type") == "thinking":
            thinking_indices.add(data["index"])
            if thinking_buffers is not None:
                thinking_buffers[data["index"]] = ""
            return None, thinking_indices

    elif t == "content_block_delta":
        idx = data.get("index")
        if idx in thinking_indices:
            if thinking_buffers is not None and idx in thinking_buffers:
                delta = data.get("delta", {})
                if isinstance(delta, dict):
                    for field in ("thinking", "signature", "thinking_delta"):
                        chunk = delta.get(field, "")
                        if chunk:
                            thinking_buffers[idx] += chunk
            return None, thinking_indices

    elif t == "content_block_stop":
        idx = data.get("index")
        if idx in thinking_indices:
            thinking_indices.discard(idx)
            return None, thinking_indices

    return line, thinking_indices


# ---- 流量捕获 ----


if DUMP_DIR:
    logger.warning("⚠ PROXY_DUMP_DIR enabled — data saved to %s", DUMP_DIR)


def _dump_json(filename: str, data):
    if not DUMP_DIR:
        return
    path = os.path.join(DUMP_DIR, filename)
    s = json.dumps(data, ensure_ascii=False, indent=2, default=str)
    if len(s) > DUMP_MAX_BYTES:
        s = s[:DUMP_MAX_BYTES] + "\n\n... [TRUNCATED at {}KB]".format(DUMP_MAX_BYTES // 1000)
    with open(path, "w") as f:
        f.write(s)
    logger.info("[CC-DUMP] %s (%d bytes)", filename, len(s))


def _summarize_request(data: dict) -> dict:
    msgs = data.get("messages", [])
    tools = data.get("tools", [])
    system = data.get("system", "")
    if isinstance(system, list):
        system = " ".join(b.get("text", "") if isinstance(b, dict) else str(b) for b in system[:2])
    return {
        "model": data.get("model", "?"),
        "stream": data.get("stream", False),
        "max_tokens": data.get("max_tokens", "?"),
        "thinking": data.get("thinking", "not set"),
        "messages": len(msgs),
        "tools": len(tools),
        "tool_names": [t.get("name", "?") for t in tools[:10]],
        "system_len": len(system),
    }


# ---- 请求处理 ----


def _build_response_headers(upstream_resp, is_sse: bool) -> dict:
    strip_keys = {"transfer-encoding", "content-encoding"}
    if is_sse:
        strip_keys.add("content-length")
    return {k: v for k, v in upstream_resp.headers.items() if k.lower() not in strip_keys}


async def proxy(request):
    method = request.method
    path = "/" + request.url.path.lstrip("/")
    upstream_url = f"{DEEPSEEK_BASE}{path}"

    headers = {k: v for k, v in request.headers.items() if k.lower() not in ("host",)}

    is_messages = method == "POST" and path.rstrip("/") == "/v1/messages"

    body = await request.body() if is_messages else b""
    modified_body = body
    strip_thinking = True

    if is_messages:
        try:
            data = json.loads(body)
            logger.info("[CC-REQ] %s", json.dumps(_summarize_request(data), ensure_ascii=False))
            _dump_json("last_request.json", data)

            original_thinking_enabled = _thinking_requested(data)

            thinking_normalized = _normalize_thinking(data)

            if _inject_thinking_blocks(data):
                logger.info("[CC-INJECT] added empty thinking block")
                thinking_normalized = True

            if original_thinking_enabled:
                strip_thinking = False
            else:
                logger.info("[CC-STRIP] response filter enabled")

            if thinking_normalized:
                modified_body = json.dumps(data, ensure_ascii=False).encode("utf-8")
                headers["content-length"] = str(len(modified_body))
                _dump_json("last_request_modified.json", data)

        except (json.JSONDecodeError, KeyError, TypeError):
            pass

    client = _get_client()

    try:
        req = client.build_request(
            method=method,
            url=upstream_url,
            headers=headers,
            content=modified_body,
        )
        upstream_resp = await client.send(req, stream=True)
    except Exception:
        logger.exception("upstream request failed: %s %s", method, upstream_url)
        return JSONResponse(
            {"error": {"message": "upstream unavailable", "type": "proxy_error"}},
            status_code=502,
        )

    content_type = upstream_resp.headers.get("content-type", "")
    is_sse = "text/event-stream" in content_type
    logger.info("[CC-RESP] status=%s sse=%s", upstream_resp.status_code, is_sse)

    if not strip_thinking or not is_sse:

        async def passthrough():
            try:
                async for chunk in upstream_resp.aiter_bytes():
                    yield chunk
            except Exception:
                logger.exception("upstream stream read error")
            finally:
                await upstream_resp.aclose()

        return StreamingResponse(
            passthrough(),
            status_code=upstream_resp.status_code,
            headers=_build_response_headers(upstream_resp, is_sse),
        )

    logger.info("[CC-FILTER] stripping thinking from SSE stream")

    async def filtered_stream():
        thinking_indices = set()
        thinking_buffers: dict[int, str] = {}
        tool_use_ids: list[str] = []
        event_types = []
        all_filtered = []
        buffer = ""

        try:
            async for chunk in upstream_resp.aiter_bytes():
                text = chunk.decode("utf-8", errors="replace")
                buffer += text

                while "\n" in buffer:
                    line, buffer = buffer.split("\n", 1)

                    if line.startswith("data: ") and len(event_types) < MAX_EVENT_TYPES:
                        try:
                            d = json.loads(line[6:])
                            event_types.append(d.get("type", "?"))
                            if d.get("type") == "content_block_start":
                                cb = d.get("content_block", {})
                                if cb.get("type") == "tool_use" and "id" in cb:
                                    tool_use_ids.append(cb["id"])
                        except json.JSONDecodeError:
                            pass

                    filtered, thinking_indices = _filter_sse_line(line, thinking_indices, thinking_buffers)
                    if filtered is not None:
                        if len(all_filtered) < MAX_FILTERED_LINES:
                            all_filtered.append(filtered)
                        yield (filtered + "\n").encode("utf-8")

            if buffer.strip():
                if buffer.startswith("data: ") and len(event_types) < MAX_EVENT_TYPES:
                    try:
                        d = json.loads(buffer[6:])
                        event_types.append(d.get("type", "?"))
                    except json.JSONDecodeError:
                        pass
                filtered, thinking_indices = _filter_sse_line(buffer, thinking_indices, thinking_buffers)
                if filtered is not None:
                    yield (filtered + "\n").encode("utf-8")

        except Exception:
            logger.exception("upstream stream read error")
        finally:
            if thinking_buffers and tool_use_ids:
                combined = "".join(thinking_buffers.values())
                if combined:
                    key = "|".join(sorted(tool_use_ids))
                    if len(_reasoning_store) >= _REASONING_STORE_LIMIT:
                        _reasoning_store.pop(next(iter(_reasoning_store)), None)
                    _reasoning_store[key] = combined
                    logger.info("[CC-REASONING] stored key=%s chars=%d", key, len(combined))
            logger.info("[CC-RESP-EVENTS] raw=%s", event_types[:LOG_EVENT_PREVIEW])
            logger.info("[CC-RESP-FILTERED] lines=%d", len(all_filtered))
            _dump_json(
                "last_response_events.json",
                {
                    "raw_events": event_types,
                    "filtered_count": len(all_filtered),
                    "first_filtered": all_filtered[:DUMP_PREVIEW_LINES],
                },
            )
            await upstream_resp.aclose()

    return StreamingResponse(
        filtered_stream(),
        status_code=upstream_resp.status_code,
        headers=_build_response_headers(upstream_resp, is_sse=True),
    )


# ---- Codex Responses API → Chat Completions 转换 ----


def _responses_to_chat(data: dict, strip_thinking: bool) -> dict:
    """将 OpenAI Responses API 请求转为 DeepSeek Chat Completions 请求。"""
    messages: list[dict] = []

    # Instructions → system message
    instructions = data.get("instructions", "")
    if isinstance(instructions, str) and instructions.strip():
        messages.append({"role": "system", "content": instructions})

    # Walk input items
    pending_reasoning: str | None = None
    pending_tool_calls: list[dict] = []

    def _flush_tool_calls():
        nonlocal pending_reasoning
        if pending_tool_calls:
            assistant_msg: dict = {
                "role": "assistant",
                "content": None,
                "tool_calls": pending_tool_calls[:],
            }
            if pending_reasoning:
                assistant_msg["reasoning_content"] = pending_reasoning
                pending_reasoning = None
            messages.append(assistant_msg)
            pending_tool_calls.clear()

    for item in data.get("input", []):
        item_type = item.get("type", "")

        if item_type == "message":
            role = item.get("role", "user")
            content_blocks = item.get("content", [])
            if isinstance(content_blocks, str):
                text = content_blocks
            else:
                text = "\n".join(
                    b.get("text", "")
                    for b in content_blocks
                    if isinstance(b, dict) and b.get("type") in ("input_text", "text")
                )
            chat_role = {"user": "user", "system": "system", "developer": "system", "assistant": "assistant"}.get(
                role, "user"
            )
            if chat_role == "assistant" and pending_reasoning:
                messages.append({"role": "assistant", "content": text, "reasoning_content": pending_reasoning})
                pending_reasoning = None
            else:
                messages.append({"role": chat_role, "content": text})

        elif item_type == "reasoning":
            summary = item.get("summary", [])
            if isinstance(summary, list):
                parts = [s.get("text", "") for s in summary if isinstance(s, dict)]
                pending_reasoning = "\n".join(parts) if parts else ""
            elif isinstance(item.get("content"), list):
                parts = [c.get("text", "") for c in item["content"] if isinstance(c, dict)]
                pending_reasoning = "\n".join(parts) if parts else ""
            else:
                pending_reasoning = item.get("content", "")

        elif item_type == "function_call":
            _flush_tool_calls()
            pending_tool_calls.append(
                {
                    "id": item.get("call_id", ""),
                    "type": "function",
                    "function": {
                        "name": item.get("name", ""),
                        "arguments": item.get("arguments", ""),
                    },
                }
            )

        elif item_type == "function_call_output":
            _flush_tool_calls()
            output = item.get("output", "")
            if isinstance(output, list):
                output = "\n".join(
                    b.get("text", "") for b in output if isinstance(b, dict) and b.get("type") in ("input_text", "text")
                )
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": item.get("call_id", ""),
                    "content": output,
                }
            )

        elif item_type == "custom_tool_call":
            _flush_tool_calls()
            pending_tool_calls.append(
                {
                    "id": item.get("call_id", ""),
                    "type": "function",
                    "function": {
                        "name": item.get("name", ""),
                        "arguments": item.get("input", ""),
                    },
                }
            )

    _flush_tool_calls()

    # reasoning.effort → reasoning_effort
    reasoning_cfg = data.get("reasoning", {})
    if isinstance(reasoning_cfg, dict):
        effort = reasoning_cfg.get("effort", "")
        effort_map = {
            "none": "none",
            "minimal": "low",
            "low": "low",
            "medium": "medium",
            "high": "high",
            "xhigh": "max",
        }
        reasoning_effort = effort_map.get(effort) if effort else None
    else:
        reasoning_effort = None

    # Tools
    tool_requests = data.get("tools", [])
    chat_tools = []
    for t in tool_requests:
        if not isinstance(t, dict):
            continue
        t_type = t.get("type", "")
        if t_type == "function":
            chat_tools.append(
                {
                    "type": "function",
                    "function": {
                        "name": t.get("name", ""),
                        "description": t.get("description", ""),
                        "parameters": t.get("parameters", {}),
                        "strict": t.get("strict"),
                    },
                }
            )
        elif t_type == "custom":
            chat_tools.append(
                {
                    "type": "function",
                    "function": {
                        "name": t.get("name", ""),
                        "description": t.get("description", ""),
                        "parameters": {"type": "object", "properties": {}, "additionalProperties": True},
                    },
                }
            )

    chat_req: dict = {
        "model": data.get("model", "deepseek-v4"),
        "messages": messages,
        "stream": data.get("stream", True),
        "temperature": data.get("temperature"),
        "top_p": data.get("top_p"),
        "max_tokens": data.get("max_output_tokens"),
        "store": data.get("store", False),
    }

    if reasoning_effort:
        chat_req["reasoning_effort"] = reasoning_effort
        chat_req["thinking"] = {"type": "enabled"}
    else:
        chat_req["thinking"] = {"type": "disabled"}

    if chat_tools:
        chat_req["tools"] = chat_tools

    tc = data.get("tool_choice")
    if isinstance(tc, str):
        chat_req["tool_choice"] = tc
    elif isinstance(tc, dict):
        chat_req["tool_choice"] = tc

    chat_req = {k: v for k, v in chat_req.items() if v is not None}

    return chat_req


# ---- Chat Completions SSE → Responses API SSE 增量转换 ----


def _chat_to_responses_sse(line: str, state: dict) -> str | None:
    """将单行 Chat Completions SSE 转为 Responses API SSE 事件。

    state 追踪转换状态: resp_id, msg_id, started, reasoning_item_added,
    tool_calls, content_started, finished
    """
    if not line.startswith("data: "):
        return line

    data_str = line[6:].strip()
    if data_str == "[DONE]":
        if not state.get("finished"):
            state["finished"] = True
            events = []
            if state.get("tool_calls"):
                for tc in state["tool_calls"].values():
                    events.append(
                        json.dumps(
                            {
                                "type": "response.function_call_arguments.done",
                                "item_id": tc["item_id"],
                                "output_index": 0,
                                "arguments": tc.get("args", ""),
                            }
                        )
                    )
                    events.append(
                        json.dumps(
                            {
                                "type": "response.output_item.done",
                                "item": {
                                    "id": tc["item_id"],
                                    "type": "function_call",
                                    "call_id": tc["id"],
                                    "name": tc["name"],
                                    "arguments": tc.get("args", ""),
                                    "status": "completed",
                                },
                                "output_index": 0,
                            }
                        )
                    )
            events.append(
                json.dumps(
                    {
                        "type": "response.completed",
                        "response": {
                            "id": state["resp_id"],
                            "object": "response",
                            "status": "completed",
                            "model": state.get("model", ""),
                        },
                    }
                )
            )
            return "data: " + "\ndata: ".join(events) + "\ndata: [DONE]"
        return None

    try:
        chunk = json.loads(data_str)
    except json.JSONDecodeError:
        return None

    choices = chunk.get("choices", [])
    if not choices:
        return None

    choice = choices[0]
    delta = choice.get("delta", {})
    finish_reason = choice.get("finish_reason")
    resp_id = state["resp_id"]
    model = chunk.get("model", "")
    state["model"] = model

    if not state.get("started"):
        state["started"] = True
        state["msg_id"] = f"msg_{resp_id[-8:]}"
        return "data: " + "\ndata: ".join(
            [
                json.dumps(
                    {
                        "type": "response.created",
                        "response": {"id": resp_id, "object": "response", "status": "in_progress", "model": model},
                    }
                ),
                json.dumps({"type": "response.in_progress", "response": {"id": resp_id}}),
            ]
        )

    reasoning = delta.get("reasoning_content", "")
    if reasoning and not state.get("reasoning_item_added"):
        state["reasoning_item_added"] = True
        return "data: " + "\ndata: ".join(
            [
                json.dumps(
                    {
                        "type": "response.output_item.added",
                        "output_index": 0,
                        "item": {"id": f"rs_{resp_id[-8:]}", "type": "reasoning", "status": "in_progress"},
                    }
                ),
                json.dumps(
                    {
                        "type": "response.content_part.added",
                        "item_id": f"rs_{resp_id[-8:]}",
                        "output_index": 0,
                        "part": {"type": "summary_text", "text": ""},
                    }
                ),
            ]
        )
    if reasoning and state.get("reasoning_item_added"):
        item_id = f"rs_{resp_id[-8:]}"
        payload = json.dumps(
            {"type": "response.output_text.delta", "item_id": item_id, "output_index": 0, "delta": reasoning}
        )
        return f"data: {payload}"

    if "tool_calls" in delta:
        for tc in delta["tool_calls"]:
            idx = tc.get("index", 0)
            tc_id = tc.get("id")
            func = tc.get("function", {})
            if tc_id is not None:
                item_id = f"fc_{resp_id[-8:]}_{idx}"
                state.setdefault("tool_calls", {})[idx] = {
                    "id": tc_id,
                    "name": func.get("name", ""),
                    "args": func.get("arguments", ""),
                    "item_id": item_id,
                }
                return "data: " + json.dumps(
                    {
                        "type": "response.output_item.added",
                        "output_index": idx,
                        "item": {
                            "id": item_id,
                            "type": "function_call",
                            "call_id": tc_id,
                            "name": func.get("name", ""),
                            "arguments": "",
                            "status": "in_progress",
                        },
                    }
                )
            else:
                tc_state = state.get("tool_calls", {}).get(idx)
                if tc_state:
                    args_delta = func.get("arguments", "")
                    tc_state["args"] += args_delta
                    payload = json.dumps(
                        {
                            "type": "response.function_call_arguments.delta",
                            "item_id": tc_state["item_id"],
                            "output_index": idx,
                            "delta": args_delta,
                        }
                    )
                    return f"data: {payload}"

    content = delta.get("content", "")
    if content and not state.get("tool_calls"):
        if not state.get("content_started"):
            state["content_started"] = True
            item_id = state.get("msg_id", f"msg_{resp_id[-8:]}")
            return "data: " + "\ndata: ".join(
                [
                    json.dumps(
                        {
                            "type": "response.output_item.added",
                            "output_index": 0,
                            "item": {"id": item_id, "type": "message", "role": "assistant", "status": "in_progress"},
                        }
                    ),
                    json.dumps(
                        {
                            "type": "response.content_part.added",
                            "item_id": item_id,
                            "output_index": 0,
                            "part": {"type": "output_text", "text": ""},
                        }
                    ),
                ]
            )
        payload = json.dumps(
            {
                "type": "response.output_text.delta",
                "item_id": state.get("msg_id", ""),
                "output_index": 0,
                "delta": content,
            }
        )
        return f"data: {payload}"

    if finish_reason in ("stop", "tool_calls"):
        state["finished"] = True
        events = []
        if not state.get("tool_calls") and not state.get("content_started"):
            # DeepSeek 可能不在 delta.content 中发文本（role chunk 等），
            # 直接补发 output_item.added 确保 Codex 收到完整的输出。
            item_id = state.get("msg_id", f"msg_{resp_id[-8:]}")
            events += [
                json.dumps(
                    {
                        "type": "response.output_item.added",
                        "output_index": 0,
                        "item": {"id": item_id, "type": "message", "role": "assistant", "status": "in_progress"},
                    }
                ),
                json.dumps(
                    {
                        "type": "response.content_part.added",
                        "item_id": item_id,
                        "output_index": 0,
                        "part": {"type": "output_text", "text": ""},
                    }
                ),
                json.dumps({"type": "response.output_text.done", "item_id": item_id, "output_index": 0, "text": ""}),
                json.dumps(
                    {
                        "type": "response.content_part.done",
                        "item_id": item_id,
                        "output_index": 0,
                        "part": {"type": "output_text", "text": ""},
                    }
                ),
                json.dumps(
                    {
                        "type": "response.output_item.done",
                        "item": {"id": item_id, "type": "message", "role": "assistant", "status": "completed"},
                        "output_index": 0,
                    }
                ),
            ]
        elif finish_reason == "stop" and not state.get("tool_calls"):
            item_id = state.get("msg_id", f"msg_{resp_id[-8:]}")
            events += [
                json.dumps({"type": "response.output_text.done", "item_id": item_id, "output_index": 0, "text": ""}),
                json.dumps(
                    {
                        "type": "response.content_part.done",
                        "item_id": item_id,
                        "output_index": 0,
                        "part": {"type": "output_text", "text": ""},
                    }
                ),
                json.dumps(
                    {
                        "type": "response.output_item.done",
                        "item": {"id": item_id, "type": "message", "role": "assistant", "status": "completed"},
                        "output_index": 0,
                    }
                ),
            ]
        for tc in state.get("tool_calls", {}).values():
            events += [
                json.dumps(
                    {
                        "type": "response.function_call_arguments.done",
                        "item_id": tc["item_id"],
                        "output_index": 0,
                        "arguments": tc.get("args", ""),
                    }
                ),
                json.dumps(
                    {
                        "type": "response.output_item.done",
                        "item": {
                            "id": tc["item_id"],
                            "type": "function_call",
                            "call_id": tc["id"],
                            "name": tc["name"],
                            "arguments": tc.get("args", ""),
                            "status": "completed",
                        },
                        "output_index": 0,
                    }
                ),
            ]
        events.append(
            json.dumps(
                {
                    "type": "response.completed",
                    "response": {"id": resp_id, "object": "response", "status": "completed", "model": model},
                }
            )
        )
        return "data: " + "\ndata: ".join(events) + "\ndata: [DONE]"
    elif finish_reason == "length":
        state["finished"] = True
        payload = json.dumps(
            {
                "type": "response.completed",
                "response": {
                    "id": resp_id,
                    "status": "incomplete",
                    "incomplete_details": {"reason": "max_output_tokens"},
                },
            }
        )
        return f"data: {payload}\ndata: [DONE]"

    return None


# ---- Codex /v1/responses 代理处理器 ----


async def proxy_responses(request):
    """处理 Codex CLI /v1/responses: 转 Responses → Chat，SSE 回转为 Responses 格式。"""
    upstream_url = f"{DEEPSEEK_CHAT_BASE}/chat/completions"
    headers = {k: v for k, v in request.headers.items() if k.lower() not in ("host",)}
    headers["content-type"] = "application/json"

    body = await request.body()
    try:
        data = json.loads(body)
    except (json.JSONDecodeError, KeyError, TypeError):
        return JSONResponse({"error": {"message": "invalid JSON body", "type": "proxy_error"}}, status_code=400)

    model = data.get("model", "")
    is_deepseek = isinstance(model, str) and model.lower().startswith("deepseek-v4")

    if not is_deepseek:
        client = _get_client()
        try:
            req = client.build_request(method="POST", url=upstream_url, headers=headers, content=body)
            upstream_resp = await client.send(req, stream=True)
        except Exception:
            logger.exception("Codex upstream request failed")
            return JSONResponse({"error": {"message": "upstream unavailable", "type": "proxy_error"}}, status_code=502)
        return StreamingResponse(
            upstream_resp.aiter_bytes(),
            status_code=upstream_resp.status_code,
            headers=_build_response_headers(upstream_resp, False),
        )

    logger.info("[CODEX-REQ] %s stream=%s", model, data.get("stream", True))
    chat_req = _responses_to_chat(data, strip_thinking=False)
    logger.info(
        "[CODEX-CHAT] %s messages=%d tools=%d",
        chat_req.get("model", "?"),
        len(chat_req.get("messages", [])),
        len(chat_req.get("tools", [])),
    )

    client = _get_client()
    body_bytes = json.dumps(chat_req, ensure_ascii=False).encode("utf-8")
    # 不传原始 content-length，httpx 会根据实际 body 自动计算
    out_headers = {k: v for k, v in headers.items() if k.lower() not in ("content-length", "transfer-encoding")}
    try:
        req = client.build_request(
            method="POST",
            url=upstream_url,
            headers=out_headers,
            content=body_bytes,
        )
        upstream_resp = await client.send(req, stream=True)
    except Exception:
        logger.exception("Codex upstream request failed")
        return JSONResponse({"error": {"message": "upstream unavailable", "type": "proxy_error"}}, status_code=502)

    is_sse = "text/event-stream" in upstream_resp.headers.get("content-type", "")
    logger.info("[CODEX-RESP] status=%s sse=%s", upstream_resp.status_code, is_sse)

    if not is_sse:

        async def _passthrough():
            try:
                async for chunk in upstream_resp.aiter_bytes():
                    yield chunk
            finally:
                await upstream_resp.aclose()

        return StreamingResponse(
            _passthrough(), status_code=upstream_resp.status_code, headers=_build_response_headers(upstream_resp, False)
        )

    import uuid

    async def _stream_convert():
        """逐 chunk 流式转换：Chat SSE → Responses SSE（参考 ai-adapter 的 ChatStreamToResponsesTranslator）。"""
        resp_id = f"resp_{uuid.uuid4().hex[:24]}"
        msg_id = f"msg_{resp_id[-8:]}"

        seq = 0
        started = False
        msg_item_added = False
        content_part_added = False
        finished = False
        current_text = ""
        current_reasoning = ""
        tool_calls: dict[int, dict] = {}  # index → {id, name, args}
        buffer = ""

        def _next_seq() -> int:
            nonlocal seq
            s = seq
            seq += 1
            return s

        _dump_path = Path.home() / ".dsv4-cc-proxy-sse-dump.txt"

        def _dump(data: bytes) -> bytes:
            with open(_dump_path, "ab") as f:
                f.write(data)
            return data

        def _emit(event: dict) -> bytes:
            event["sequence_number"] = _next_seq()
            return _dump(f"data: {json.dumps(event)}\n\n".encode())

        try:
            async for data in upstream_resp.aiter_bytes():
                text = data.decode("utf-8", errors="replace")
                buffer += text
                while "\n" in buffer:
                    line, buffer = buffer.split("\n", 1)
                    stripped = line.strip()
                    if not stripped.startswith("data: ") or stripped == "data: [DONE]":
                        continue
                    try:
                        chunk = json.loads(stripped[6:])
                    except json.JSONDecodeError:
                        continue

                    choices = chunk.get("choices")
                    if not choices:
                        continue
                    choice = choices[0]
                    delta = choice.get("delta", {})
                    model = chunk.get("model", "")

                    # 首次 chunk：emit response.created + response.in_progress
                    if not started:
                        started = True
                        yield _emit(
                            {
                                "type": "response.created",
                                "response": {
                                    "id": resp_id,
                                    "object": "response",
                                    "status": "in_progress",
                                    "model": model,
                                },
                            }
                        )
                        yield _emit({"type": "response.in_progress", "response": {"id": resp_id}})

                    # 处理 reasoning_content
                    if delta.get("reasoning_content"):
                        rc = delta["reasoning_content"]
                        if not current_reasoning:
                            rs_id = f"rs_{resp_id[-8:]}"
                            yield _emit(
                                {
                                    "type": "response.output_item.added",
                                    "output_index": 0,
                                    "item": {"id": rs_id, "type": "reasoning", "status": "in_progress"},
                                }
                            )
                            yield _emit(
                                {
                                    "type": "response.content_part.added",
                                    "item_id": rs_id,
                                    "output_index": 0,
                                    "content_index": 0,
                                    "part": {"type": "summary_text", "text": ""},
                                }
                            )
                        current_reasoning += rc
                        yield _emit(
                            {
                                "type": "response.output_text.delta",
                                "item_id": f"rs_{resp_id[-8:]}",
                                "output_index": 0,
                                "content_index": 0,
                                "delta": rc,
                            }
                        )

                    # 处理 tool_calls
                    for tc in delta.get("tool_calls", []):
                        idx = tc.get("index", 0)
                        tc_id = tc.get("id")
                        func = tc.get("function", {})
                        if tc_id is not None:
                            item_id = f"fc_{resp_id[-8:]}_{idx}"
                            tool_calls[idx] = {
                                "id": tc_id,
                                "name": func.get("name", ""),
                                "args": func.get("arguments", ""),
                                "item_id": item_id,
                            }
                            yield _emit(
                                {
                                    "type": "response.output_item.added",
                                    "output_index": idx,
                                    "item": {
                                        "id": item_id,
                                        "type": "function_call",
                                        "call_id": tc_id,
                                        "name": func.get("name", ""),
                                        "arguments": "",
                                        "status": "in_progress",
                                    },
                                }
                            )
                        else:
                            tc = tool_calls.get(idx)
                            if tc:
                                args_delta = func.get("arguments", "")
                                tc["args"] += args_delta
                                yield _emit(
                                    {
                                        "type": "response.function_call_arguments.delta",
                                        "item_id": tc["item_id"],
                                        "output_index": idx,
                                        "delta": args_delta,
                                    }
                                )

                    # 处理 text content
                    if delta.get("content"):
                        ct = delta["content"]
                        if not msg_item_added:
                            msg_item_added = True
                            yield _emit(
                                {
                                    "type": "response.output_item.added",
                                    "output_index": 0,
                                    "item": {
                                        "id": msg_id,
                                        "type": "message",
                                        "role": "assistant",
                                        "status": "in_progress",
                                    },
                                }
                            )
                        if not content_part_added:
                            content_part_added = True
                            yield _emit(
                                {
                                    "type": "response.content_part.added",
                                    "item_id": msg_id,
                                    "output_index": 0,
                                    "content_index": 0,
                                    "part": {"type": "output_text", "text": ""},
                                }
                            )
                        current_text += ct
                        yield _emit(
                            {
                                "type": "response.output_text.delta",
                                "item_id": msg_id,
                                "output_index": 0,
                                "content_index": 0,
                                "delta": ct,
                            }
                        )

                    # 处理 finish_reason
                    fr = choice.get("finish_reason")
                    if fr in ("stop", "tool_calls", "length"):
                        finished = True

                        # reasoning done
                        if current_reasoning:
                            rs_id = f"rs_{resp_id[-8:]}"
                            for ev in [
                                {
                                    "type": "response.output_text.done",
                                    "item_id": rs_id,
                                    "output_index": 0,
                                    "text": current_reasoning,
                                },
                                {
                                    "type": "response.content_part.done",
                                    "item_id": rs_id,
                                    "output_index": 0,
                                    "part": {"type": "summary_text", "text": current_reasoning},
                                },
                                {
                                    "type": "response.output_item.done",
                                    "item": {"id": rs_id, "type": "reasoning", "status": "completed"},
                                    "output_index": 0,
                                },
                            ]:
                                yield _emit(ev)

                        # text message done
                        if msg_item_added:
                            for ev in [
                                {"type": "response.output_text.done", "item_id": msg_id, "output_index": 0, "text": ""},
                                {
                                    "type": "response.content_part.done",
                                    "item_id": msg_id,
                                    "output_index": 0,
                                    "part": {"type": "output_text", "text": ""},
                                },
                                {
                                    "type": "response.output_item.done",
                                    "item": {
                                        "id": msg_id,
                                        "type": "message",
                                        "role": "assistant",
                                        "status": "completed",
                                    },
                                    "output_index": 0,
                                },
                            ]:
                                yield _emit(ev)

                        # tool calls done
                        for tc in tool_calls.values():
                            for ev in [
                                {
                                    "type": "response.function_call_arguments.done",
                                    "item_id": tc["item_id"],
                                    "output_index": 0,
                                    "arguments": tc["args"],
                                },
                                {
                                    "type": "response.output_item.done",
                                    "item": {
                                        "id": tc["item_id"],
                                        "type": "function_call",
                                        "call_id": tc["id"],
                                        "name": tc["name"],
                                        "arguments": tc["args"],
                                        "status": "completed",
                                    },
                                    "output_index": 0,
                                },
                            ]:
                                yield _emit(ev)

                        status = "completed" if fr != "length" else "incomplete"
                        yield _emit(
                            {
                                "type": "response.completed",
                                "response": {"id": resp_id, "object": "response", "status": status, "model": model},
                            }
                        )

                        logger.info(
                            "[CODEX-SSE] finished — text=%d chars, tools=%d", len(current_text), len(tool_calls)
                        )
                        yield _dump(b"data: [DONE]\n\n")

            # 流结束时补发 response.completed（上游没发 finish_reason 的情况）
            if not finished and started:
                if current_reasoning:
                    rs_id = f"rs_{resp_id[-8:]}"
                    for ev in [
                        {
                            "type": "response.output_text.done",
                            "item_id": rs_id,
                            "output_index": 0,
                            "text": current_reasoning,
                        },
                        {
                            "type": "response.content_part.done",
                            "item_id": rs_id,
                            "output_index": 0,
                            "part": {"type": "summary_text", "text": current_reasoning},
                        },
                        {
                            "type": "response.output_item.done",
                            "item": {"id": rs_id, "type": "reasoning", "status": "completed"},
                            "output_index": 0,
                        },
                    ]:
                        yield _emit(ev)
                if msg_item_added:
                    for ev in [
                        {"type": "response.output_text.done", "item_id": msg_id, "output_index": 0, "text": ""},
                        {
                            "type": "response.content_part.done",
                            "item_id": msg_id,
                            "output_index": 0,
                            "part": {"type": "output_text", "text": ""},
                        },
                        {
                            "type": "response.output_item.done",
                            "item": {"id": msg_id, "type": "message", "role": "assistant", "status": "completed"},
                            "output_index": 0,
                        },
                    ]:
                        yield _emit(ev)
                yield _emit(
                    {
                        "type": "response.completed",
                        "response": {
                            "id": resp_id,
                            "object": "response",
                            "status": "completed",
                            "model": chat_req.get("model", ""),
                        },
                    }
                )
                logger.info("[CODEX-SSE] stream-ended without finish_reason — forced complete")
                yield _dump(b"data: [DONE]\n\n")

        except Exception:
            logger.exception("Codex SSE stream error")
        finally:
            await upstream_resp.aclose()

    return StreamingResponse(
        _stream_convert(),
        status_code=upstream_resp.status_code,
        headers=_build_response_headers(upstream_resp, is_sse=True),
    )


# ---- Codex /v1/chat/completions 代理处理器 ----


async def proxy_chat(request):
    """处理 Codex CLI wire_api=chat 模式: 对 DeepSeek 做 thinking 标准化后直通。"""
    upstream_url = f"{DEEPSEEK_CHAT_BASE}/chat/completions"
    headers = {k: v for k, v in request.headers.items() if k.lower() not in ("host",)}
    headers["content-type"] = "application/json"

    body = await request.body()
    try:
        data = json.loads(body)
    except (json.JSONDecodeError, KeyError, TypeError):
        return JSONResponse({"error": {"message": "invalid JSON body", "type": "proxy_error"}}, status_code=400)

    model = data.get("model", "")
    is_deepseek = isinstance(model, str) and model.lower().startswith("deepseek-v4")

    if not is_deepseek:
        client = _get_client()
        try:
            req = client.build_request(method="POST", url=upstream_url, headers=headers, content=body)
            upstream_resp = await client.send(req, stream=True)
        except Exception:
            logger.exception("Chat upstream request failed")
            return JSONResponse({"error": {"message": "upstream unavailable", "type": "proxy_error"}}, status_code=502)
        return StreamingResponse(
            upstream_resp.aiter_bytes(),
            status_code=upstream_resp.status_code,
            headers=_build_response_headers(upstream_resp, False),
        )

    logger.info("[CHAT-REQ] %s stream=%s", model, data.get("stream", True))

    modified = False
    thinking_cfg = data.get("thinking", {})

    if isinstance(thinking_cfg, dict) and thinking_cfg.get("type") == "adaptive":
        data["thinking"] = {"type": "disabled"}
        modified = True
        logger.info("[CHAT-THINKING] adaptive → disabled")

    if "reasoning_effort" in data:
        val = data.pop("reasoning_effort")
        logger.info("[CHAT] removed reasoning_effort=%s", val)
        modified = True

    modified_body = json.dumps(data, ensure_ascii=False).encode("utf-8") if modified else body
    if modified:
        headers["content-length"] = str(len(modified_body))

    client = _get_client()
    try:
        req = client.build_request(method="POST", url=upstream_url, headers=headers, content=modified_body)
        upstream_resp = await client.send(req, stream=True)
    except Exception:
        logger.exception("Chat upstream request failed")
        return JSONResponse({"error": {"message": "upstream unavailable", "type": "proxy_error"}}, status_code=502)

    is_sse = "text/event-stream" in upstream_resp.headers.get("content-type", "")
    thinking_enabled = isinstance(thinking_cfg, dict) and thinking_cfg.get("type") == "enabled"
    logger.info("[CHAT-RESP] status=%s sse=%s thinking_enabled=%s", upstream_resp.status_code, is_sse, thinking_enabled)

    if not is_sse or thinking_enabled:

        async def _passthrough():
            try:
                async for chunk in upstream_resp.aiter_bytes():
                    yield chunk
            finally:
                await upstream_resp.aclose()

        return StreamingResponse(
            _passthrough(),
            status_code=upstream_resp.status_code,
            headers=_build_response_headers(upstream_resp, is_sse),
        )

    logger.info("[CHAT-STRIP] removing reasoning_content from stream")

    async def _chat_filtered():
        buffer = ""
        try:
            async for chunk in upstream_resp.aiter_bytes():
                text = chunk.decode("utf-8", errors="replace")
                buffer += text
                while "\n" in buffer:
                    line, buffer = buffer.split("\n", 1)
                    if line.startswith("data: ") and line != "data: [DONE]":
                        try:
                            d = json.loads(line[6:])
                            choices = d.get("choices", [])
                            if choices:
                                delta = choices[0].get("delta", {})
                                delta.pop("reasoning_content", None)
                                if not delta:
                                    continue
                            line = "data: " + json.dumps(d, ensure_ascii=False)
                        except json.JSONDecodeError:
                            pass
                    yield (line + "\n").encode("utf-8")
            if buffer.strip():
                yield (buffer + "\n").encode("utf-8")
        except Exception:
            logger.exception("Chat stream read error")
        finally:
            await upstream_resp.aclose()

    return StreamingResponse(
        _chat_filtered(),
        status_code=upstream_resp.status_code,
        headers=_build_response_headers(upstream_resp, is_sse=True),
    )


# ---- 应用工厂 ----


@asynccontextmanager
async def lifespan(app):
    logger.info("started v%s (upstream=%s)", VERSION, DEEPSEEK_BASE)
    yield
    logger.info("shutting down")
    if _shared_client and not _shared_client.is_closed:
        await _shared_client.aclose()


def create_app() -> Starlette:
    return Starlette(
        lifespan=lifespan,
        routes=[
            Route("/health", health, methods=["GET"]),
            Route("/v1/responses", proxy_responses, methods=["POST"]),
            Route("/v1/chat/completions", proxy_chat, methods=["POST"]),
            Route("/{path:path}", proxy, methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"]),
        ],
    )
