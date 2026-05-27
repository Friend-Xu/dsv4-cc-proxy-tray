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

import httpx
from starlette.applications import Starlette
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route

from dsv4_cc_proxy._version import VERSION

# ---- 配置 ----

DEEPSEEK_BASE = os.getenv("PROXY_UPSTREAM", "https://api.deepseek.com/anthropic")
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
    ids = sorted(
        b["id"] for b in content
        if isinstance(b, dict) and b.get("type") == "tool_use" and "id" in b
    )
    return "|".join(ids) if ids else None


# ---- 健康检查 ----


async def health(request):
    return JSONResponse({
        "status": "ok",
        "version": VERSION,
        "upstream": DEEPSEEK_BASE,
    })


# ---- 修复 1: 请求端 thinking 注入 ----


def _has_tool_use(content: list) -> bool:
    return any(
        isinstance(b, dict) and b.get("type") == "tool_use" for b in content
    )


def _has_thinking(content: list) -> bool:
    return any(
        isinstance(b, dict) and b.get("type") in ("thinking", "redacted_thinking")
        for b in content
    )


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
                        logger.info("[REASONING] injected stored reasoning key=%s", key)
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
            logger.info("[THINKING] removed %s=%s", key, val)

    stripped = 0
    for msg in data.get("messages", []):
        if msg.get("role") != "assistant":
            continue
        content = msg.get("content", [])
        if isinstance(content, str):
            continue
        new_content = [
            b for b in content
            if not (isinstance(b, dict) and b.get("type") in ("thinking", "redacted_thinking"))
        ]
        if len(new_content) != len(content):
            stripped += len(content) - len(new_content)
            msg["content"] = new_content

    logger.info(
        "[THINKING] converted %s → disabled, stripped %d thinking blocks",
        thinking_type, stripped,
    )
    return True


# ---- 修复 3: 响应端 thinking 剥离 ----


def _thinking_requested(data: dict) -> bool:
    thinking_cfg = data.get("thinking", {})
    return (
        isinstance(thinking_cfg, dict)
        and thinking_cfg.get("type") == "enabled"
    )


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
    logger.info("[DUMP] %s (%d bytes)", filename, len(s))


def _summarize_request(data: dict) -> dict:
    msgs = data.get("messages", [])
    tools = data.get("tools", [])
    system = data.get("system", "")
    if isinstance(system, list):
        system = " ".join(
            b.get("text", "") if isinstance(b, dict) else str(b)
            for b in system[:2]
        )
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
    return {
        k: v for k, v in upstream_resp.headers.items()
        if k.lower() not in strip_keys
    }


async def proxy(request):
    method = request.method
    path = "/" + request.url.path.lstrip("/")
    upstream_url = f"{DEEPSEEK_BASE}{path}"

    headers = {k: v for k, v in request.headers.items()
               if k.lower() not in ("host",)}

    is_messages = (method == "POST" and path.rstrip("/") == "/v1/messages")

    body = await request.body() if is_messages else b""
    modified_body = body
    strip_thinking = True

    if is_messages:
        try:
            data = json.loads(body)
            logger.info("[REQ] %s", json.dumps(_summarize_request(data), ensure_ascii=False))
            _dump_json("last_request.json", data)

            original_thinking_enabled = _thinking_requested(data)

            thinking_normalized = _normalize_thinking(data)

            if _inject_thinking_blocks(data):
                logger.info("[INJECT] added empty thinking block")
                thinking_normalized = True

            if original_thinking_enabled:
                strip_thinking = False
            else:
                logger.info("[STRIP] response filter enabled")

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
    logger.info("[RESP] status=%s sse=%s", upstream_resp.status_code, is_sse)

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

    logger.info("[FILTER] stripping thinking from SSE stream")

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

                    filtered, thinking_indices = _filter_sse_line(
                        line, thinking_indices, thinking_buffers
                    )
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
                filtered, thinking_indices = _filter_sse_line(
                    buffer, thinking_indices, thinking_buffers
                )
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
                    logger.info("[REASONING] stored key=%s chars=%d", key, len(combined))
            logger.info("[RESP-EVENTS] raw=%s", event_types[:LOG_EVENT_PREVIEW])
            logger.info("[RESP-FILTERED] lines=%d", len(all_filtered))
            _dump_json("last_response_events.json", {
                "raw_events": event_types,
                "filtered_count": len(all_filtered),
                "first_filtered": all_filtered[:DUMP_PREVIEW_LINES],
            })
            await upstream_resp.aclose()

    return StreamingResponse(
        filtered_stream(),
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
            Route("/{path:path}", proxy, methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"]),
        ],
    )
