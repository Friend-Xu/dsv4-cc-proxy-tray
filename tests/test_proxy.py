"""dsv4-cc-proxy 单元测试。

覆盖 pure functions: 请求端注入、thinking 标准化、SSE 过滤、reasoning 记忆-回注。
运行: python3 -m pytest tests/test_proxy.py -v
"""

import json

from dsv4_cc_proxy.proxy import (
    _chat_to_responses_sse,
    _filter_sse_line,
    _has_thinking,
    _has_tool_use,
    _inject_thinking_blocks,
    _normalize_thinking,
    _reasoning_store,
    _responses_to_chat,
    _thinking_requested,
    _tool_use_key,
)

# === 辅助函数 ===


def test_has_tool_use():
    assert _has_tool_use([{"type": "tool_use", "name": "Bash"}])
    assert not _has_tool_use([{"type": "text", "text": "hello"}])
    assert not _has_tool_use([])
    assert _has_tool_use([{"type": "text"}, {"type": "tool_use"}])


def test_has_thinking():
    assert _has_thinking([{"type": "thinking", "thinking": ""}])
    assert _has_thinking([{"type": "redacted_thinking", "data": "..."}])
    assert not _has_thinking([{"type": "tool_use"}])
    assert not _has_thinking([])


# === 修复 1: thinking 注入 ===


def test_inject_thinking_disabled():
    data = {
        "model": "deepseek-v4-pro",
        "thinking": {"type": "disabled"},
        "messages": [
            {"role": "assistant", "content": [{"type": "tool_use", "id": "call_1", "name": "Bash", "input": {}}]}
        ],
    }
    assert not _inject_thinking_blocks(data)


def test_inject_thinking_non_v4():
    data = {
        "model": "claude-sonnet-4-6",
        "thinking": {"type": "enabled"},
        "messages": [
            {"role": "assistant", "content": [{"type": "tool_use", "id": "call_1", "name": "Bash", "input": {}}]}
        ],
    }
    assert not _inject_thinking_blocks(data)


def test_inject_thinking_adds_block():
    data = {
        "model": "deepseek-v4-pro",
        "thinking": {"type": "enabled"},
        "messages": [
            {
                "role": "assistant",
                "content": [{"type": "tool_use", "id": "call_1", "name": "Bash", "input": {"cmd": "ls"}}],
            }
        ],
    }
    assert _inject_thinking_blocks(data)
    content = data["messages"][0]["content"]
    assert content[0]["type"] == "thinking"
    assert content[0]["thinking"] == ""
    assert content[1]["type"] == "tool_use"


def test_inject_thinking_skips_when_has_thinking():
    data = {
        "model": "deepseek-v4-pro",
        "thinking": {"type": "enabled"},
        "messages": [
            {
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": "already there"},
                    {"type": "tool_use", "id": "call_1", "name": "Bash", "input": {}},
                ],
            }
        ],
    }
    data_copy = json.loads(json.dumps(data))
    assert not _inject_thinking_blocks(data)
    assert data == data_copy


def test_inject_thinking_string_content():
    data = {
        "model": "deepseek-v4-pro",
        "thinking": {"type": "enabled"},
        "messages": [{"role": "assistant", "content": "plain text content"}],
    }
    assert not _inject_thinking_blocks(data)


# === 修复 2: thinking 标准化 ===


def test_normalize_enabled_unchanged():
    data = {"thinking": {"type": "enabled"}}
    assert not _normalize_thinking(data)
    assert data["thinking"]["type"] == "enabled"


def test_normalize_disabled_unchanged():
    data = {"thinking": {"type": "disabled"}}
    assert not _normalize_thinking(data)
    assert data["thinking"]["type"] == "disabled"


def test_normalize_adaptive_converts():
    data = {
        "thinking": {"type": "adaptive"},
        "messages": [
            {
                "role": "assistant",
                "content": [{"type": "thinking", "thinking": "some thought"}, {"type": "text", "text": "hello"}],
            }
        ],
    }
    assert _normalize_thinking(data)
    assert data["thinking"]["type"] == "disabled"
    content = data["messages"][0]["content"]
    assert len(content) == 1
    assert content[0]["type"] == "text"


def test_normalize_adaptive_removes_effort():
    data = {"thinking": {"type": "adaptive"}, "reasoning_effort": "max"}
    _normalize_thinking(data)
    assert "reasoning_effort" not in data
    assert data["thinking"]["type"] == "disabled"


def test_normalize_adaptive_removes_output_config():
    data = {"thinking": {"type": "adaptive"}, "output_config": {"effort": "max"}}
    _normalize_thinking(data)
    assert "output_config" not in data


def test_normalize_no_thinking_key():
    assert not _normalize_thinking({"max_tokens": 100})


# === 修复 3: SSE 过滤 ===


def test_filter_sse_passes_non_data():
    assert _filter_sse_line("event: message_start", set()) == ("event: message_start", set())
    assert _filter_sse_line("", set()) == ("", set())
    assert _filter_sse_line(":comment", set()) == (":comment", set())


def test_filter_sse_passes_text():
    result, _ = _filter_sse_line(
        'data: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}', set()
    )
    assert result is not None


def test_filter_sse_passes_tool_use():
    result, _ = _filter_sse_line(
        'data: {"type":"content_block_start","index":1,"content_block":{"type":"tool_use",'
        '"id":"call_1","name":"Bash","input":{}}}',
        set(),
    )
    assert result is not None


def test_filter_sse_filters_thinking_start():
    idx = set()
    result, idx = _filter_sse_line(
        'data: {"type":"content_block_start","index":0,"content_block":'
        '{"type":"thinking","thinking":"","signature":""}}',
        idx,
    )
    assert result is None
    assert 0 in idx


def test_filter_sse_filters_thinking_delta():
    idx = {0}
    result, idx = _filter_sse_line(
        'data: {"type":"content_block_delta","index":0,"delta":{"type":"thinking_delta","thinking":"Hello"}}', idx
    )
    assert result is None


def test_filter_sse_filters_signature_delta():
    idx = {0}
    result, idx = _filter_sse_line(
        'data: {"type":"content_block_delta","index":0,"delta":{"type":"signature_delta","signature":"abc"}}', idx
    )
    assert result is None


def test_filter_sse_full_thinking_block():
    idx = set()
    lines = [
        'data: {"type":"content_block_start","index":0,"content_block":'
        '{"type":"thinking","thinking":"","signature":""}}',
        'data: {"type":"content_block_delta","index":0,"delta":{"type":"thinking_delta","thinking":"x"}}',
        'data: {"type":"content_block_delta","index":0,"delta":{"type":"signature_delta","signature":"sig"}}',
        'data: {"type":"content_block_stop","index":0}',
        'data: {"type":"content_block_start","index":1,"content_block":{"type":"text","text":""}}',
    ]
    results = []
    for line in lines:
        filtered, idx = _filter_sse_line(line, idx)
        results.append(filtered)
    assert results == [None, None, None, None, lines[4]]


def test_filter_sse_invalid_json():
    result, _ = _filter_sse_line("data: {invalid json}", set())
    assert result == "data: {invalid json}"


# === thinking_requested ===


def test_thinking_requested():
    assert _thinking_requested({"thinking": {"type": "enabled"}})
    assert not _thinking_requested({"thinking": {"type": "disabled"}})
    assert not _thinking_requested({"thinking": {"type": "adaptive"}})
    assert not _thinking_requested({})


# === 修复 4: reasoning 记忆-回注 ===


def test_tool_use_key():
    content = [
        {"type": "tool_use", "id": "call_2", "name": "Bash"},
        {"type": "tool_use", "id": "call_1", "name": "Read"},
    ]
    assert _tool_use_key(content) == "call_1|call_2"
    assert _tool_use_key([{"type": "text", "text": "hi"}]) is None
    assert _tool_use_key([]) is None


def test_inject_thinking_from_store():
    _reasoning_store.clear()
    _reasoning_store["call_1"] = "Let me think about this..."

    data = {
        "model": "deepseek-v4-pro",
        "thinking": {"type": "enabled"},
        "messages": [
            {"role": "assistant", "content": [{"type": "tool_use", "id": "call_1", "name": "Bash", "input": {}}]}
        ],
    }
    assert _inject_thinking_blocks(data)
    content = data["messages"][0]["content"]
    assert content[0]["type"] == "thinking"
    assert content[0]["thinking"] == "Let me think about this..."

    _reasoning_store.clear()


def test_filter_sse_captures_thinking_content():
    idx = set()
    buf: dict[int, str] = {}

    # start thinking block
    line, idx = _filter_sse_line(
        'data: {"type":"content_block_start","index":0,"content_block":'
        '{"type":"thinking","thinking":"","signature":""}}',
        idx,
        buf,
    )
    assert line is None
    assert buf.get(0) == ""

    # thinking delta
    line, idx = _filter_sse_line(
        'data: {"type":"content_block_delta","index":0,"delta":{"type":"thinking_delta","thinking":"Hello"}}', idx, buf
    )
    assert line is None
    assert buf.get(0) == "Hello"

    # signature delta
    line, idx = _filter_sse_line(
        'data: {"type":"content_block_delta","index":0,"delta":{"type":"signature_delta","signature":"ABC123"}}',
        idx,
        buf,
    )
    assert line is None
    assert buf.get(0) == "HelloABC123"

    # stop
    line, idx = _filter_sse_line('data: {"type":"content_block_stop","index":0}', idx, buf)
    assert line is None
    assert 0 not in idx


# === Codex Responses → Chat 转换测试 ===


def test_responses_to_chat_basic_message():
    req = {
        "model": "deepseek-v4",
        "input": [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hello"}]}],
        "stream": True,
    }
    chat = _responses_to_chat(req, strip_thinking=False)
    assert chat["model"] == "deepseek-v4"
    assert chat["stream"] is True
    assert chat["messages"][0]["role"] == "user"
    assert chat["messages"][0]["content"] == "hello"
    assert chat["thinking"] == {"type": "disabled"}


def test_responses_to_chat_instructions():
    req = {
        "model": "deepseek-v4",
        "instructions": "You are helpful.",
        "input": [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hi"}]}],
    }
    chat = _responses_to_chat(req, strip_thinking=False)
    assert chat["messages"][0]["role"] == "system"
    assert chat["messages"][0]["content"] == "You are helpful."


def test_responses_to_chat_reasoning_effort():
    req = {
        "model": "deepseek-v4",
        "input": [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": "x"}]}],
        "reasoning": {"effort": "high"},
    }
    chat = _responses_to_chat(req, strip_thinking=False)
    assert chat["reasoning_effort"] == "high"
    assert chat["thinking"] == {"type": "enabled"}


def test_responses_to_chat_function_call():
    req = {
        "model": "deepseek-v4",
        "input": [
            {"type": "function_call", "call_id": "fc1", "name": "read_file", "arguments": '{"path":"/x"}'},
        ],
        "tools": [{"type": "function", "name": "read_file", "parameters": {}}],
    }
    chat = _responses_to_chat(req, strip_thinking=False)
    assistant = chat["messages"][0]
    assert assistant["role"] == "assistant"
    assert assistant["tool_calls"][0]["function"]["name"] == "read_file"
    assert len(chat["tools"]) == 1


def test_responses_to_chat_tool_roundtrip():
    req = {
        "model": "deepseek-v4",
        "input": [
            {"type": "function_call", "call_id": "c1", "name": "f", "arguments": "{}"},
            {"type": "function_call_output", "call_id": "c1", "output": "result"},
        ],
    }
    chat = _responses_to_chat(req, strip_thinking=False)
    roles = [m["role"] for m in chat["messages"]]
    assert roles == ["assistant", "tool"]


# === Chat SSE → Responses SSE 转换测试 ===


def test_chat_to_responses_sse_start():
    state: dict = {"resp_id": "resp_test1234567890abcdef1234"}
    r = _chat_to_responses_sse(
        'data: {"choices":[{"delta":{},"index":0}],"model":"deepseek-v4"}',
        state,
    )
    assert r is not None
    assert "response.created" in r
    assert "response.in_progress" in r
    assert state["started"] is True


def test_chat_to_responses_sse_text_delta():
    state: dict = {
        "resp_id": "resp_test1234567890abcdef1234",
        "started": True,
        "msg_id": "msg_12345678",
        "content_started": False,
    }
    # First text chunk should emit output_item.added + content_part.added
    r = _chat_to_responses_sse(
        'data: {"choices":[{"delta":{"content":"Hello"},"index":0}]}',
        state,
    )
    assert r is not None
    assert "response.output_item.added" in r
    assert "response.content_part.added" in r
    assert state["content_started"] is True

    # Second chunk
    r = _chat_to_responses_sse(
        'data: {"choices":[{"delta":{"content":" world"},"index":0}]}',
        state,
    )
    assert r is not None
    assert "response.output_text.delta" in r


def test_chat_to_responses_sse_finish():
    state: dict = {
        "resp_id": "resp_test1234567890abcdef1234",
        "started": True,
        "msg_id": "msg_12345678",
        "content_started": True,
        "finished": False,
    }
    r = _chat_to_responses_sse(
        'data: {"choices":[{"delta":{},"index":0,"finish_reason":"stop"}],"model":"deepseek-v4"}',
        state,
    )
    assert r is not None
    assert "response.output_text.done" in r
    assert "response.completed" in r
    assert state["finished"] is True


def test_chat_to_responses_sse_done_skips():
    state: dict = {"resp_id": "x", "finished": True}
    r = _chat_to_responses_sse("data: [DONE]", state)
    assert r is None


def test_chat_to_responses_sse_non_data():
    state: dict = {"resp_id": "x"}
    r = _chat_to_responses_sse(":keepalive\n", state)
    assert r == ":keepalive\n"
