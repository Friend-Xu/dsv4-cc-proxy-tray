<div align="center">

[**中文版 README**](README.zh-CN.md)

# dsv4-cc-proxy-tray

**Make DeepSeek V4 work flawlessly with Claude Code on Windows**

Anthropic API compatibility proxy with a native Windows GUI — one-click launch, no terminal needed.

> **源仓库:** [github.com/HosheaLi/dsv4-cc-proxy](https://github.com/HosheaLi/dsv4-cc-proxy)

```
Claude Code ←→ localhost:16889 (dsv4-cc-proxy) ←→ api.deepseek.com/anthropic
```

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue)](https://www.python.org)
[![Platform](https://img.shields.io/badge/platform-Windows-blue)]()

![screenshot](screenshot.png)

</div>

---

## Why dsv4-cc-proxy

DeepSeek V4 implements the Anthropic API format, but has 3 critical incompatibilities that break Claude Code. This proxy fixes them transparently.

| # | Problem | Symptom | Fix |
|---|---------|---------|-----|
| 1 | `tool_use` assistant messages missing a `thinking` block | `reasoning_content` 400 error | Inject empty thinking block before each tool_use |
| 2 | DeepSeek unconditionally emits `thinking`/`signature_delta` SSE events even when thinking is disabled | `Tool result missing due to internal error` in Claude Code | Strip thinking events from the SSE response stream |
| 3 | `thinking.type=adaptive` (Claude Code default) + `reasoning_effort` not supported by DeepSeek | Stream truncation / 400 errors | Normalize to `disabled` + strip reasoning_effort |

Non-DeepSeek requests and non-`/messages` endpoints pass through with zero overhead.

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
# 或直接双击
scripts\start_gui.bat
```

### Configure Claude Code

Point Claude Code to the proxy by adding to your `settings.local.json`:

```json
"ANTHROPIC_BASE_URL": "http://localhost:16889"
```

## GUI Features

- **Start / Stop** proxy with one click
- **Real-time colored log** display with auto-scroll
- **Config panel** — upstream URL, listen address, log level
- **Persistent settings** saved to `~/.dsv4-cc-proxy-tray.json`
- **Cross-platform process management** — works on Windows without POSIX signals

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
| Non-messages endpoints | — | Zero-overhead passthrough |
| Non-DeepSeek models | — | Zero-overhead passthrough |

## How It Works

```
┌─────────────┐     ┌──────────────────┐     ┌────────────────────┐
│ Claude Code │ ──→ │  dsv4-cc-proxy   │ ──→ │  api.deepseek.com  │
│             │     │  localhost:16889  │     │  /anthropic        │
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
                   └────────────────┘
```

## Project Structure

```
.
├── dsv4_cc_proxy/
│   ├── __init__.py            # Package entry
│   ├── __main__.py            # CLI entry
│   ├── _version.py            # VERSION = "1.8.0"
│   ├── proxy.py               # Core proxy logic
│   └── gui.py                 # Windows GUI launcher
├── tests/
│   └── test_proxy.py          # 25 unit tests
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
