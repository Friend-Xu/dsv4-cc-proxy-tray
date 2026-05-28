<div align="center">

[**中文版 README**](README.zh-CN.md)

# dsv4-cc-proxy-tray

**Make DeepSeek V4 work flawlessly with Claude Code & Codex CLI on Windows**

Anthropic API + OpenAI Responses API compatibility proxy with a native Windows GUI — one-click launch, no terminal needed.

> **源仓库:** [github.com/HosheaLi/dsv4-cc-proxy](https://github.com/HosheaLi/dsv4-cc-proxy)

```
Claude Code ←→ localhost:16889 /v1/messages    ──→ api.deepseek.com/anthropic
Codex CLI   ←→ localhost:16889 /v1/responses   ──→ api.deepseek.com/v1/chat/completions
Codex CLI   ←→ localhost:16889 /v1/chat/completions ──→ api.deepseek.com/v1/chat/completions
```

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue)](https://www.python.org)
[![Platform](https://img.shields.io/badge/platform-Windows-blue)]()

![screenshot](screenshot.png)

</div>

---

## Why dsv4-cc-proxy

DeepSeek V4 has protocol incompatibilities that break both Claude Code and Codex CLI. This proxy fixes them transparently with zero-config auto-routing.

### Claude Code fixes (Anthropic Messages API)

| # | Problem | Symptom | Fix |
|---|---------|---------|-----|
| 1 | `tool_use` assistant messages missing a `thinking` block | `reasoning_content` 400 error | Inject empty thinking block before each tool_use |
| 2 | DeepSeek unconditionally emits `thinking`/`signature_delta` SSE events even when thinking is disabled | `Tool result missing due to internal error` in Claude Code | Strip thinking events from the SSE response stream |
| 3 | `thinking.type=adaptive` (Claude Code default) + `reasoning_effort` not supported by DeepSeek | Stream truncation / 400 errors | Normalize to `disabled` + strip reasoning_effort |

### Codex CLI fixes (OpenAI Responses API)

| # | Problem | Symptom | Fix |
|---|---------|---------|-----|
| 4 | Codex speaks Responses API (`/v1/responses`) but DeepSeek only provides Chat Completions | 404 or protocol mismatch | Convert Responses → Chat requests + SSE back-translation |
| 5 | DeepSeek Chat API emits `reasoning_content` in SSE stream | Codex may reject unexpected fields | Strip `reasoning_content` from Chat SSE stream |

Non-DeepSeek requests pass through with zero overhead.

## Quick Start

### Option 1: Download exe (recommended for Windows)

Download `dsv4-cc-proxy-tray.exe` from [Releases](https://github.com/Friend-Xu/dsv4-cc-proxy/releases), double-click to run.

- **No Python required** — self-contained, all dependencies bundled
- **No black console window** — clean GUI only
- **No pip install** — just download and run

### Option 2: Run from source

```bash
pip install -e .
python dsv4_cc_proxy/gui.py
# or double-click
scripts\start_gui.bat
```

### Configure Claude Code

Point Claude Code to the proxy by adding to your `settings.local.json`:

```json
"ANTHROPIC_BASE_URL": "http://localhost:16889"
```

### Configure Codex CLI

Point Codex CLI to the proxy by editing `~/.codex/config.toml`:

```toml
openai_base_url = "http://localhost:16889/v1"
model = "deepseek-v4-pro"
```

The proxy auto-detects the request path and applies the correct fixes — no mode switching needed.

## GUI Features

- **Start / Stop** proxy with one click
- **Real-time colored log** display with auto-scroll
- **Config panel** — upstream URL, listen address, log level
- **Persistent settings** saved to `~/.dsv4-cc-proxy-tray.json`
- **Cross-platform process management** — works on Windows without POSIX signals
- **Auto-routing** — serves both Claude Code and Codex CLI simultaneously

## Configuration

| Env Var | Default | Description |
|---------|---------|-------------|
| `PROXY_UPSTREAM` | `https://api.deepseek.com/anthropic` | DeepSeek API base URL |
| `PROXY_HOST` | `127.0.0.1` | Bind address |
| `PROXY_PORT` | `16889` | Bind port |
| `PROXY_LOG_LEVEL` | `warning` | Log level (`info` for debugging) |
| `PROXY_DUMP_DIR` | *(empty=off)* | Debug traffic dump directory. ⚠ Contains conversation data |

## Comparison

| Scenario | Without Proxy | With Proxy |
|----------|--------------|------------|
| tool_use msg without thinking | 400 error | Auto-injected empty thinking |
| Claude Code sends `thinking.type=adaptive` | Stream truncation / 400 | Normalized to `disabled` |
| DeepSeek SSE thinking events | Tool result missing error | Silently stripped from stream |
| Codex `/v1/responses` → DeepSeek | 404 / protocol mismatch | Converted to Chat + SSE back-translated |
| DeepSeek Chat `reasoning_content` in SSE | Codex rejects | Silently stripped |
| Non-DeepSeek models / endpoints | — | Zero-overhead passthrough |

## How It Works

```
┌─────────────┐     ┌──────────────────┐     ┌────────────────────┐
│ Claude Code │ ──→ │  dsv4-cc-proxy   │ ──→ │  api.deepseek.com  │
│             │     │  localhost:16889  │     │  /anthropic        │
│ Codex CLI   │ ──→ │                  │ ──→ │  /v1/chat/complet. │
└─────────────┘     └──────────────────┘     └────────────────────┘
                           │
                   ┌───────┴────────┐
                   │  Fixes applied  │
                   │  1. Thinking     │
                   │     injection   │
                   │  2. Thinking     │
                   │     normalize   │
                   │  3. SSE events   │
                   │     strip       │
                   │  4. Responses↔  │
                   │     Chat convert│
                   └────────────────┘
```

**Route dispatch** — the proxy registers 3 routes on port 16889:

| Route | Target Client | Processing |
|-------|---------------|------------|
| `POST /v1/messages` | Claude Code | Thinking injection + normalize + SSE strip |
| `POST /v1/responses` | Codex CLI (default) | Responses → Chat request convert + SSE back-translate |
| `POST /v1/chat/completions` | Codex CLI (`wire_api=chat`) | Thinking normalize + reasoning_content strip |
| `/*` (catch-all) | Everything else | Zero-overhead passthrough |

## Project Structure

```
.
├── dsv4_cc_proxy/
│   ├── __init__.py            # Package entry
│   ├── __main__.py            # CLI entry
│   ├── _version.py            # VERSION = "1.8.0"
│   ├── proxy.py               # Core proxy logic (3 routes + 5 fixes)
│   └── gui.py                 # Windows GUI launcher
├── tests/
│   └── test_proxy.py          # 35 unit tests
├── scripts/
│   ├── build_exe.bat          # PyInstaller packaging
│   ├── start_gui.bat          # Dev launch script
│   ├── start.bat              # CLI startup
│   └── start.ps1              # PowerShell startup
├── pyproject.toml
├── .github/workflows/ci.yml
└── LICENSE
```

## Building from Source

```bash
# Install dev dependencies
pip install -e ".[test]"

# Run tests
pytest tests/ -v

# Build exe
scripts\build_exe.bat
```

## Health Check

```bash
curl http://localhost:16889/health
# → {"status":"ok","version":"1.8.0","upstream":"https://api.deepseek.com/anthropic"}
```

## License

[MIT](LICENSE)
