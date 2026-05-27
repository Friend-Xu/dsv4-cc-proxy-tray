# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project identity

**DeepSeek Anthropic API 兼容性代理** — a transparent local proxy that fixes 3 DeepSeek V4 incompatibilities so Claude Code can use DeepSeek models via the Anthropic API. Proxies `POST /v1/messages`; all other requests pass through with zero overhead.

## Commands

```bash
# Install in dev mode
pip install -e ".[test]"

# Run all tests
pytest tests/ -v

# Run a single test
pytest tests/test_proxy.py::test_inject_thinking_adds_block -v

# Lint
ruff check dsv4_cc_proxy/ tests/

# Start the proxy (after pip install, or use `python -m dsv4_cc_proxy`)
dsv4-cc-proxy

# Stop the proxy
dsv4-cc-proxy --stop

# Build Docker image
docker build -t dsv4-cc-proxy .
```

## Architecture

**Source layout** — the package is `dsv4_cc_proxy/`, not `proxy/`. Three files:

- `_version.py` — single source of truth for `VERSION = "1.8.0"` (read by `pyproject.toml` at build time)
- `proxy.py` — all proxy logic, exports `create_app()` factory
- `__main__.py` — CLI (`dsv4-cc-proxy` command), handles PID file management and starts `uvicorn` with `factory=True`

**Request flow** (only for `POST /v1/messages` targeting `deepseek-v4*` models):

1. **Request injection** (`_inject_thinking_blocks`): When `thinking.type=enabled`, assistant messages with `tool_use` blocks but no `thinking` block get an empty `{"type": "thinking", "thinking": ""}` inserted before the first `tool_use`.
2. **Request normalization** (`_normalize_thinking`): Converts `thinking.type=adaptive` (Claude Code default, unsupported by DeepSeek) to `disabled`, strips `reasoning_effort` and `output_config` keys, and removes any existing `thinking`/`redacted_thinking` blocks from assistant content.
3. **Response SSE filtering** (`_filter_sse_line`): Tracks `content_block_start` indices where `type=thinking` and filters subsequent `content_block_delta`/`content_block_stop` events for those indices. Only active when the client did not request thinking (`_thinking_requested` returns false).

**Response strategy**: When thinking filtering is needed, the proxy uses `_filter_sse_line` to skip thinking events from the upstream SSE stream. When stripping is not needed (non-DeepSeek models, or client explicitly enabled thinking), use raw passthrough with `aiter_bytes()`.

**Key design decisions**:
- Catch-all route `/{path:path}` handles all HTTP methods — anything not matching `/health` falls through to the proxy
- Singleton `httpx.AsyncClient` reused across requests (`_get_client()`) with 600s timeout
- Version imported from `_version.py` at import time, not read from file
- PID file in `/tmp/dsv4-cc-proxy.pid` prevents duplicate instances; `--stop` sends SIGTERM with SIGKILL fallback after 5s
