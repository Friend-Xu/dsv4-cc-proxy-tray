<div align="center">

# dsv4-cc-proxy-tray

**让 DeepSeek V4 在 Windows 上与 Claude Code 无缝配合**

Anthropic API 兼容性代理，自带 Windows 原生图形界面 — 一键启动，无需终端。

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

## 为什么需要这个代理

DeepSeek V4 实现了 Anthropic API 格式，但有 3 个关键的不兼容问题会导致 Claude Code 无法正常运行。这个代理在中间透明地修复它们。

| # | 问题 | 症状 | 修复 |
|---|------|------|------|
| 1 | tool_use assistant 消息缺少 thinking 块 | `reasoning_content` 400 错误 | 在每个 tool_use 前注入空 thinking 块 |
| 2 | DeepSeek 无条件返回 thinking/signature_delta SSE 事件 | Claude Code 报 `Tool result missing due to internal error` | 从 SSE 响应流中剥离 thinking 事件 |
| 3 | `thinking.type=adaptive`（Claude Code 默认值）+ `reasoning_effort` 不被 DeepSeek 支持 | 流式截断 / 400 错误 | 标准化为 `disabled` + 移除 reasoning_effort |

非 DeepSeek 模型请求和非 `/messages` 端点的请求零开销透传。

## 快速开始

### 方式一：下载 exe（推荐）

从 [Releases](https://github.com/Friend-Xu/dsv4-cc-proxy/releases) 下载 `dsv4-cc-proxy-tray.exe`，双击运行。

- **无需安装 Python** — 所有依赖已打包在内
- **无黑窗** — 纯净图形界面
- **开箱即用** — 无需 pip install

### 方式二：从源码运行

```bash
pip install -e .
python dsv4_cc_proxy/gui.py
```

### 配置 Claude Code

在 `settings.local.json` 中添加：

```json
"ANTHROPIC_BASE_URL": "http://localhost:16889"
```

## 图形界面功能介绍

- **一键启停** 代理服务
- **实时彩色日志** 显示，自动滚动、按日志级别着色
- **配置面板** — 上游地址、监听地址、日志级别
- **持久化配置** 保存到 `~/.dsv4-cc-proxy-tray.json`
- **跨平台进程管理** — Windows 下使用 `taskkill` 替代 POSIX 信号

## 配置

| 环境变量 | 默认值 | 说明 |
|----------|--------|------|
| `PROXY_UPSTREAM` | `https://api.deepseek.com/anthropic` | DeepSeek API 地址 |
| `PROXY_HOST` | `127.0.0.1` | 监听地址 |
| `PROXY_PORT` | `16889` | 监听端口 |
| `PROXY_LOG_LEVEL` | `warning` | 日志级别（调试用 `info`） |
| `PROXY_DUMP_DIR` | *(空=关闭)* | 流量捕获目录。⚠ 含用户对话数据 |

## 效果对比

| 场景 | 无代理 | 有代理 |
|------|--------|--------|
| tool_use 消息缺少 thinking | 400 错误 | 自动注入空 thinking |
| Claude Code 发送 `thinking.type=adaptive` | 流截断 / 400 | 标准化为 disabled |
| DeepSeek 返回 thinking SSE 事件 | Tool result missing 错误 | 静默剥离 |
| 非 messages 端点 | — | 零开销透传 |
| 非 DeepSeek 模型 | — | 零开销透传 |

## 工作原理

```
┌─────────────┐     ┌──────────────────┐     ┌────────────────────┐
│ Claude Code │ ──→ │  dsv4-cc-proxy   │ ──→ │  api.deepseek.com  │
│             │     │  localhost:16889  │     │  /anthropic        │
└─────────────┘     └──────────────────┘     └────────────────────┘
                           │
                   ┌───────┴────────┐
                   │  三层修复        │
                   │  1. thinking    │
                   │     注入        │
                   │  2. thinking    │
                   │     标准化      │
                   │  3. SSE 事件    │
                   │     剥离        │
                   └────────────────┘
```

代理拦截 `POST /v1/messages`，对 `deepseek-v4*` 模型做三层处理，其他请求透明透传。

## 目录结构

```
.
├── dsv4_cc_proxy/
│   ├── __init__.py            # 包入口
│   ├── __main__.py            # CLI 入口
│   ├── _version.py            # VERSION = "1.8.0"
│   ├── proxy.py               # 核心代理逻辑
│   └── gui.py                 # Windows GUI 启动器
├── tests/
│   └── test_proxy.py          # 25 个单元测试
├── scripts/
│   ├── build_exe.bat          # PyInstaller 打包脚本
│   ├── start_gui.bat          # 开发环境启动
│   ├── start.bat              # 命令行启动
│   └── start.ps1              # PowerShell 启动
├── pyproject.toml
├── .github/workflows/ci.yml
└── LICENSE
```

## 从源码构建

```bash
# 安装开发依赖
pip install -e ".[test]"

# 运行测试
pytest tests/ -v

# 构建 exe
scripts\build_exe.bat
```

## 健康检查

```bash
curl http://localhost:16889/health
# → {"status":"ok","version":"1.8.0","upstream":"https://api.deepseek.com/anthropic"}
```

## 许可证

[MIT](LICENSE)
