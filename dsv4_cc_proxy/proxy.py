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

# ==================== 新增：提取 system 消息的函数 ====================
def _extract_system_messages(data: dict) -> dict:
    """
    将 Claude Code 新版请求中的 system 消息提回顶层 (修复 role: system 错误)
    """
    messages = data.get("messages", [])
    if not isinstance(messages, list):
        return data

    system_content = []
    new_messages = []
    for msg in messages:
        if msg.get("role") == "system":
            # 提取 system 内容
            content = msg.get("content", "")
            if content:
                system_content.append(content)
        else:
            # 保留 user / assistant 消息
            new_messages.append(msg)

    # 合并顶层的 system 字段（如果有）
    old_system = data.get("system", "")
    if system_content:
        combined_system = "\n".join(system_content)
        if old_system:
            combined_system = old_system + "\n" + combined_system
        data["system"] = combined_system
    elif old_system:
        data["system"] = old_system
    else:
        data.pop("system", None)

    data["messages"] = new_messages
    return data
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

            data = _extract_system_messages(data)

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
    """将 OpenAI Responses API 请求转为 DeepSeek Chat Completions 请求。

    基于 Nigel211/codex_deepseek_proxy 的 extract_messages 重写，
    关键要点:
    - function_call 只累积不 flush (避免不完整配对)
    - message 处理前先 flush 累积的 function_calls
    - message.content 中也要提取 tool_call 类型块
    - 末尾重排: system/developer 消息移到 assistant(tool_calls) 之前
    """
    ROLE_MAP = {"developer": "system"}
    messages: list[dict] = []

    instructions = data.get("instructions", "")
    if isinstance(instructions, str) and instructions.strip():
        messages.append({"role": "system", "content": instructions})

    pending_reasoning: str = ""
    pending_tool_calls: list[dict] = []

    def _flush_tool_calls():
        nonlocal pending_reasoning
        if pending_tool_calls:
            msg: dict = {
                "role": "assistant",
                "content": "",
                "tool_calls": pending_tool_calls[:],
            }
            if pending_reasoning:
                msg["reasoning_content"] = pending_reasoning
                pending_reasoning = ""
            messages.append(msg)
            pending_tool_calls.clear()

    for item in data.get("input", []):
        if not isinstance(item, dict):
            continue
        item_type = item.get("type", "")

        # --- message ---
        if item_type == "message":
            _flush_tool_calls()  # 先把前面累积的 function_calls 提交
            role = item.get("role", "user")
            role = ROLE_MAP.get(role, role)
            content = item.get("content", "")

            if isinstance(content, list):
                texts: list[str] = []
                tool_calls_from_content: list[dict] = []
                for c in content:
                    if not isinstance(c, dict):
                        continue
                    c_type = c.get("type", "")
                    if c_type in ("text", "input_text", "output_text"):
                        t = c.get("text", "") or ""
                        if t.strip():
                            texts.append(t)
                    elif c_type == "tool_call":
                        tool_calls_from_content.append({
                            "id": c.get("id", ""),
                            "type": "function",
                            "function": {
                                "name": c.get("name", ""),
                                "arguments": c.get("arguments", ""),
                            },
                        })
                text = "\n".join(texts)
                if tool_calls_from_content:
                    msg: dict = {"role": role, "content": text or ""}
                    msg["tool_calls"] = tool_calls_from_content
                    if item.get("reasoning_content"):
                        msg["reasoning_content"] = item["reasoning_content"]
                    messages.append(msg)
                elif text:
                    msg = {"role": role, "content": text}
                    if item.get("reasoning_content"):
                        msg["reasoning_content"] = item["reasoning_content"]
                    messages.append(msg)
                # 如果 content 列表没有文本也没有 tool_calls，跳过
            elif isinstance(content, str):
                text = content.strip()
                if text:
                    msg = {"role": role, "content": text}
                    if item.get("reasoning_content"):
                        msg["reasoning_content"] = item["reasoning_content"]
                    messages.append(msg)

        # --- function_call ---
        elif item_type == "function_call":
            pending_tool_calls.append({
                "id": item.get("call_id", ""),
                "type": "function",
                "function": {
                    "name": item.get("name", ""),
                    "arguments": item.get("arguments", ""),
                },
            })
            if item.get("reasoning_content") and not pending_reasoning:
                pending_reasoning = item["reasoning_content"]

        # --- function_call_output ---
        elif item_type == "function_call_output":
            _flush_tool_calls()
            output = item.get("output", "")
            if isinstance(output, list):
                output = "\n".join(
                    b.get("text", "") for b in output
                    if isinstance(b, dict) and b.get("type") in ("input_text", "text")
                )
            messages.append({
                "role": "tool",
                "tool_call_id": item.get("call_id", ""),
                "content": output,
            })

        # --- reasoning ---
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

        # --- custom_tool_call ---
        elif item_type == "custom_tool_call":
            pending_tool_calls.append({
                "id": item.get("call_id", ""),
                "type": "function",
                "function": {
                    "name": item.get("name", ""),
                    "arguments": item.get("input", ""),
                },
            })

    _flush_tool_calls()

    # ---- 重排消息：确保 tool 消息紧跟对应的 assistant 消息 ----
    # DeepSeek 要求：带 tool_calls 的 assistant 消息后必须紧跟所有对应的 tool 消息。
    # Codex 有时会在中间注入 system/developer 消息，把它们移到 assistant 之前。
    reordered: list[dict] = []
    i = 0
    while i < len(messages):
        msg = messages[i]
        if msg.get("role") == "assistant" and msg.get("tool_calls"):
            expected_ids = {tc["id"] for tc in msg["tool_calls"]}
            tool_msgs: list[dict] = []
            pre_msgs: list[dict] = []
            j = i + 1
            while j < len(messages) and expected_ids:
                nxt = messages[j]
                if nxt.get("role") == "tool" and nxt.get("tool_call_id") in expected_ids:
                    expected_ids.remove(nxt["tool_call_id"])
                    tool_msgs.append(nxt)
                elif nxt.get("role") in ("system", "developer"):
                    pre_msgs.append(nxt)
                else:
                    break
                j += 1
            reordered.extend(pre_msgs)
            reordered.append(msg)
            reordered.extend(tool_msgs)
            i = j
        else:
            reordered.append(msg)
            i += 1
    messages = reordered

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
                            "part": {"type": "text", "text": ""},
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
                        "part": {"type": "text", "text": ""},
                    }
                ),
                json.dumps({"type": "response.output_text.done", "item_id": item_id, "output_index": 0, "text": ""}),
                json.dumps(
                    {
                        "type": "response.content_part.done",
                        "item_id": item_id,
                        "output_index": 0,
                        "part": {"type": "text", "text": ""},
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
                        "part": {"type": "text", "text": ""},
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
        """Chat SSE → Responses SSE，按 Nigel211/codex_deepseek_proxy 的已验证格式。"""
        resp_id = f"resp_{uuid.uuid4().hex[:12]}"
        text_item_id = f"item_{uuid.uuid4().hex[:12]}"

        started = False
        finished = False
        full_text = ""
        full_reasoning = ""
        has_text = False
        text_started = False
        tool_calls: dict[int, dict] = {}  # index → {id, name, arguments, item_id, started}
        seq = 0
        buffer = ""

        _dump_path = Path.home() / ".dsv4-cc-proxy-sse-dump.txt"

        def _emit(event: dict) -> bytes:
            nonlocal seq
            ev_type = event.get("type", "")
            # Nigel211 只在 text delta 上加 sequence_number
            if ev_type == "response.output_text.delta" and "sequence_number" not in event:
                seq += 1
                event["sequence_number"] = seq
            payload = json.dumps(event, ensure_ascii=False)
            data = f"event: {ev_type}\ndata: {payload}\n\n".encode() if ev_type else f"data: {payload}\n\n".encode()
            with open(_dump_path, "ab") as f:
                f.write(data)
            return data

        try:
            async for chunk_data in upstream_resp.aiter_bytes():
                text = chunk_data.decode("utf-8", errors="replace")
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

                    if "error" in chunk:
                        err = chunk["error"]
                        message = err.get("message", str(err))
                        logger.error("[CODEX-SSE] upstream error: %s", message)
                        yield _emit({
                            "type": "response.failed",
                            "response": {"id": resp_id, "object": "response", "status": "failed",
                                         "model": chat_req.get("model", ""), "error": {"message": message, "type": "upstream_error"},
                                         "output": [], "usage": None},
                        })
                        finished = True
                        return

                    choices = chunk.get("choices")
                    if not choices:
                        continue
                    choice = choices[0]
                    delta = choice.get("delta", {})
                    model = chunk.get("model", "")
                    finish_reason = choice.get("finish_reason")

                    # ---- 首次：emit response.created + response.in_progress ----
                    if not started:
                        started = True
                        base = {"id": resp_id, "object": "response", "status": "in_progress",
                                "model": model, "output": [], "usage": None}
                        yield _emit({"type": "response.created", "response": base})
                        yield _emit({"type": "response.in_progress", "response": base})

                    # ---- reasoning_content（存储，不单独 emit reasoning item）----
                    rc = delta.get("reasoning_content", "")
                    if rc:
                        full_reasoning += rc

                    # ---- 文本内容 ----
                    content = delta.get("content", "")
                    if content:
                        if not text_started:
                            text_started = True
                            has_text = True
                            yield _emit({
                                "type": "response.output_item.added", "output_index": 0,
                                "item": {"id": text_item_id, "type": "message", "status": "in_progress",
                                         "role": "assistant", "content": []},
                            })
                            yield _emit({
                                "type": "response.content_part.added", "item_id": text_item_id,
                                "output_index": 0, "content_index": 0,
                                "part": {"type": "text", "text": ""},
                            })
                        full_text += content
                        yield _emit({
                            "type": "response.output_text.delta", "delta": content,
                            "item_id": text_item_id, "output_index": 0, "content_index": 0,
                        })

                    # ---- 工具调用 ----
                    for tc in delta.get("tool_calls", []):
                        idx = tc.get("index", 0)
                        if idx not in tool_calls:
                            item_id = f"item_{uuid.uuid4().hex[:12]}"
                            tool_calls[idx] = {"id": "", "name": "", "arguments": "", "item_id": item_id, "started": False}
                        acc = tool_calls[idx]
                        if tc.get("id"):
                            acc["id"] = tc["id"]
                        func = tc.get("function", {})
                        if func.get("name"):
                            acc["name"] = func["name"]
                        args_delta = func.get("arguments", "")
                        if args_delta:
                            acc["arguments"] += args_delta
                            out_idx = (1 if has_text else 0) + sorted(tool_calls.keys()).index(idx)
                            if not acc["started"]:
                                acc["started"] = True
                                yield _emit({
                                    "type": "response.output_item.added", "output_index": out_idx,
                                    "item": {"id": acc["item_id"], "type": "function_call",
                                             "status": "in_progress", "call_id": acc["id"],
                                             "name": acc["name"], "arguments": ""},
                                })
                            yield _emit({
                                "type": "response.function_call_arguments.delta",
                                "item_id": acc["item_id"], "output_index": out_idx,
                                "delta": args_delta,
                            })

                    # ---- 完成事件 ----
                    if finish_reason in ("stop", "tool_calls", "length"):
                        finished = True
                        status = "completed" if finish_reason != "length" else "incomplete"

                        # 文本完成
                        if has_text:
                            yield _emit({"type": "response.output_text.done", "text": full_text,
                                         "item_id": text_item_id, "output_index": 0, "content_index": 0})
                            yield _emit({"type": "response.content_part.done", "item_id": text_item_id,
                                         "output_index": 0, "content_index": 0,
                                         "part": {"type": "text", "text": full_text}})
                            text_item = {"id": text_item_id, "type": "message", "status": "completed",
                                         "role": "assistant", "content": [{"type": "text", "text": full_text}]}
                            if full_reasoning:
                                text_item["reasoning_content"] = full_reasoning
                            yield _emit({"type": "response.output_item.done", "output_index": 0, "item": text_item})

                        # 工具调用完成
                        output_items = []
                        if has_text:
                            ti = {"id": text_item_id, "type": "message", "status": "completed",
                                  "role": "assistant", "content": [{"type": "text", "text": full_text}]}
                            if full_reasoning:
                                ti["reasoning_content"] = full_reasoning
                            output_items.append(ti)

                        for idx in sorted(tool_calls.keys()):
                            acc = tool_calls[idx]
                            out_idx = (1 if has_text else 0) + sorted(tool_calls.keys()).index(idx)
                            yield _emit({"type": "response.function_call_arguments.done",
                                         "item_id": acc["item_id"], "output_index": out_idx,
                                         "arguments": acc["arguments"]})
                            func_item = {"id": acc["item_id"], "type": "function_call",
                                         "status": "completed", "call_id": acc["id"],
                                         "name": acc["name"], "arguments": acc["arguments"]}
                            if full_reasoning:
                                func_item["reasoning_content"] = full_reasoning
                            yield _emit({"type": "response.output_item.done", "output_index": out_idx, "item": func_item})
                            output_items.append({"id": acc["item_id"], "type": "function_call",
                                                "status": "completed", "call_id": acc["id"],
                                                "name": acc["name"], "arguments": acc["arguments"]})

                        # response.completed
                        yield _emit({"type": "response.completed",
                                     "response": {"id": resp_id, "object": "response", "status": status,
                                                  "model": model, "output": output_items, "usage": None}})

                        logger.info("[CODEX-SSE] finished — text=%d chars, tools=%d", len(full_text), len(tool_calls))
                        yield f"data: [DONE]\n\n".encode()

            # 流结束但无 finish_reason — 补发完成
            if not finished and started:
                if has_text:
                    yield _emit({"type": "response.output_text.done", "text": full_text,
                                 "item_id": text_item_id, "output_index": 0, "content_index": 0})
                    yield _emit({"type": "response.content_part.done", "item_id": text_item_id,
                                 "output_index": 0, "content_index": 0,
                                 "part": {"type": "text", "text": full_text}})
                    text_item = {"id": text_item_id, "type": "message", "status": "completed",
                                 "role": "assistant", "content": [{"type": "text", "text": full_text}]}
                    yield _emit({"type": "response.output_item.done", "output_index": 0, "item": text_item})
                for idx in sorted(tool_calls.keys()):
                    acc = tool_calls[idx]
                    out_idx = (1 if has_text else 0) + sorted(tool_calls.keys()).index(idx)
                    yield _emit({"type": "response.function_call_arguments.done",
                                 "item_id": acc["item_id"], "output_index": out_idx, "arguments": acc["arguments"]})
                    yield _emit({"type": "response.output_item.done", "output_index": out_idx,
                                 "item": {"id": acc["item_id"], "type": "function_call", "status": "completed",
                                          "call_id": acc["id"], "name": acc["name"], "arguments": acc["arguments"]}})
                yield _emit({"type": "response.completed",
                             "response": {"id": resp_id, "object": "response", "status": "completed",
                                          "model": chat_req.get("model", ""), "output": [], "usage": None}})
                logger.info("[CODEX-SSE] stream-ended without finish_reason — forced complete")
                yield f"data: [DONE]\n\n".encode()

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

    # ---- 修复 Chat Completions messages（Nigel211 风格）----
    # DeepSeek 要求: assistant(tool_calls) 后紧跟所有对应 tool 消息，
    # content 不能为 null，system 消息不能插在 assistant 和 tool 之间。
    msgs = data.get("messages", [])
    if msgs:
        # 1. 修复 content: null → ""
        for m in msgs:
            if isinstance(m, dict) and m.get("role") == "assistant" and m.get("tool_calls"):
                if m.get("content") is None:
                    m["content"] = ""
        # 2. 重排：system 消息移到 assistant(tool_calls) 前，确保 tool 紧跟
        reordered: list[dict] = []
        i = 0
        while i < len(msgs):
            m = msgs[i]
            if isinstance(m, dict) and m.get("role") == "assistant" and m.get("tool_calls"):
                expected_ids = {tc["id"] for tc in m["tool_calls"] if isinstance(tc, dict) and "id" in tc}
                tool_msgs: list[dict] = []
                pre_msgs: list[dict] = []
                j = i + 1
                while j < len(msgs) and expected_ids:
                    nxt = msgs[j]
                    if isinstance(nxt, dict) and nxt.get("role") == "tool" and nxt.get("tool_call_id") in expected_ids:
                        expected_ids.remove(nxt["tool_call_id"])
                        tool_msgs.append(nxt)
                    elif isinstance(nxt, dict) and nxt.get("role") in ("system", "developer"):
                        pre_msgs.append(nxt)
                    else:
                        break
                    j += 1
                reordered.extend(pre_msgs)
                reordered.append(m)
                reordered.extend(tool_msgs)
                i = j
            else:
                reordered.append(m)
                i += 1
        data["messages"] = reordered
        modified = True

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
