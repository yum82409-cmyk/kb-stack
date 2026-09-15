#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
mcp_server.py v2 —— 个人代码知识库 MCP 服务端（stdio 传输）
================================================================
把 kb_rag.py 的检索/问答能力注册为编辑器 AI 可调用的两个工具：
  · search_knowledge_base(query, top_k)  纯向量检索，秒回，供 AI 阅读原文
  · ask_knowledge_base(query, top_k)     检索 + 本地 qwen3 总结（附来源）

== stdio 纯净度防护（v2 核心增强）================================
MCP stdio 传输中 stdout 是 JSON-RPC 独占管道：任何一行非 JSON 输出
（ChromaDB banner、库的 print、警告）都会让客户端解析崩溃。
本服务做了两层防护：
  1.【文件描述符级】服务启动后立即把 OS 层 fd 1（stdout）重定向到
     stderr（fd 2），再把 Python 层 sys.stdout 指向原始 fd 1 的副本。
     效果：任何绕过 sys.stdout 直接写 fd 1 的 C 扩展/子库输出
     （如 onnxruntime、sqlite3 的底层警告）都被物理导流到 stderr；
     而官方 SDK 拿到的 sys.stdout 仍是干净的协议管道。
  2.【上下文管理器级】每个工具执行体包在 redirect_stdout(→stderr)
     里，捕获 Python 层的意外 print；FastMCP 的日志本身走 stderr。
实测（Windows/Python 3.11/mcp 1.28.1）：注入脏 print 的压力测试下
stdout 输出的每一行均可 json.loads 解析。

== 启动配置（编辑器接入）=========================================
VS Code（Cline 插件，settings.json → cline.mcpServers）或
Cursor（~/.cursor/mcp.json），模板见同目录 mcp-config-template.json：

{
  "mcpServers": {
    "kb-rag": {
      "command": "<ABSOLUTE_PATH_TO_YOUR_PYTHON>",
      "args": ["<ABSOLUTE_PATH_TO_REPO>/mcp_server.py"],
      "env": { "OLLAMA_HOST": "127.0.0.1:11434", "PYTHONIOENCODING": "utf-8" }
    }
  }
}

  · command   = Python 解释器绝对路径（`python -c "import sys; print(sys.executable)"` 获取）
  · args      = 本文件绝对路径
  · env       = 显式指定 Ollama 地址（防系统 OLLAMA_HOST=0.0.0.0 干扰）

== 依赖安装 ======================================================
    pip install mcp chromadb requests

== 已知平台坑（前轮实测定位，保留备忘）===========================
chromadb 与运行中的 anyio 事件循环共存在本机会死锁（import 挂起 120s+）。
对策：在 mcp.run() 之前、事件循环未启动时完成 chromadb 导入与集合
预热（见下方 PREWARM 段）。此设计不可删除。
"""

from __future__ import annotations

import contextlib
import os
import sys
from pathlib import Path

# ---------- 第 1 层防护：fd 级 stdout 导流（必须在所有重库 import 之前）----------
def _sanitize_fds() -> None:
    """OS 层把 fd 1 重定向到 stderr，并给 sys.stdout 换上「双面替身」。

    背景（读 mcp 1.28.1 源码得出）：FastMCP 的 stdio 传输用
    TextIOWrapper(sys.stdout.buffer) 写协议 —— 协议输出和普通 print
    共用同一个 sys.stdout 对象。所以防护必须精确区分两种用法：
      · .buffer  → 保留给协议 fd（SDK 写 JSON-RPC 专用）
      · .write() → 转投 stderr（任何 Python print / 库输出）
    另外 fd 1 本身被 dup2 到 stderr：绕过 sys.stdout 直接写 fd 1 的
    C 扩展输出（onnxruntime、sqlite3 底层告警等）也被物理导流。

    压力测试（本仓库实测）：print / os.write(1,...) / sys.__stdout__
    三种脏输出全部落 stderr，stdout 每行均可 json.loads。
    """
    sys.stdout.flush()
    sys.stderr.flush()
    protocol_fd = os.dup(1)               # 保留干净的协议通道
    os.dup2(2, 1)                         # fd 1 → fd 2（C 扩展脏输出进 stderr）

    import io

    class ProtocolSafeStdout(io.TextIOWrapper):
        """双面 stdout：write 给 print 用（→stderr），buffer 给协议用（→协议 fd）。"""
        def write(self, s: str) -> int:
            return sys.stderr.write(s)
        def flush(self) -> None:
            sys.stderr.flush()
        # 继承自 TextIOWrapper 的 .buffer 指向 protocol_fd 的二进制流，
        # SDK 的 TextIOWrapper(sys.stdout.buffer) 包的是它 —— 协议照常。

    # 先用协议 fd 建立二进制层，再包成替身文本层
    proto_bin = os.fdopen(protocol_fd, "wb", buffering=0)
    sys.stdout = ProtocolSafeStdout(proto_bin, encoding="utf-8", newline="\n",
                                     write_through=True, line_buffering=True)


_sanitize_fds()

# 让脚本从任意工作目录/解释器启动都能找到同目录的 kb_rag
sys.path.insert(0, str(Path(__file__).resolve().parent))

import requests  # noqa: E402 （kb_rag 依赖；import 失败在此尽早报错并走 stderr）
from mcp.server.fastmcp import FastMCP  # noqa: E402

import kb_rag  # noqa: E402

# 规范化 Ollama 地址（处理环境变量 OLLAMA_HOST=0.0.0.0 之类的写法）
kb_rag.normalize_ollama_host()

# ---- ChromaDB 预热（事件循环启动前，防死锁，见文件头注释）----
# import chromadb 时若其内部输出任何 banner，已被第 1 层防护导流到 stderr。
import chromadb  # noqa: E402
_COL_PREWARM = chromadb.PersistentClient(path=kb_rag.CHROMA_DIR).get_collection("kb-projects")

mcp = FastMCP("kb-rag", log_level="WARNING")


# ---------- 第 2 层防护：工具执行期的 stdout 捕获 ----------
@contextlib.contextmanager
def _clean_stdout():
    """工具执行体专用：Python 层的意外 print 重定向到 stderr。"""
    with contextlib.redirect_stdout(sys.stderr):
        yield


@mcp.tool()
def search_knowledge_base(query: str, top_k: int = 5) -> str:
    """在个人代码知识库（半成品项目复盘文档）中做语义检索，返回匹配度最高的若干
    代码片段或复盘文档原文，供 AI 直接阅读。不调用大模型，速度快。
    适用：快速定位某个项目/模块/接口记录在哪个文档里。
    参数 query：检索词，中英文均可（如"电机驱动 PWM"或"登录模块的实现"）。
    参数 top_k：返回片段数，默认 5，最大 20。"""
    with _clean_stdout():
        try:
            return kb_rag.search_text(query, top_k=max(1, min(int(top_k), 20)))
        except SystemExit as e:          # kb_rag 对「库为空」等情况抛 SystemExit
            return f"[检索失败] {e}"
        except Exception as e:           # 任何异常都转成文本结果，绝不向上炸协议层
            return f"[工具执行错误] {type(e).__name__}: {e}"


@mcp.tool()
def ask_knowledge_base(query: str, top_k: int = 5) -> str:
    """向「项目代码助教」提问：先检索知识库相关片段，再由本地大模型（qwen3:8b）
    生成针对历史半成品项目的进度总结或开发建议，回答末尾附参考来源文件。
    适用：询问某半成品项目的开发进度、缺失模块、核心接口、重启思路。
    参数 query：自然语言问题（如"总结 monitor-lab 的开发进度和缺失模块"）。
    参数 top_k：检索召回的片段数，默认 5，最大 20。
    注意：本地模型生成需要 10-60 秒，请耐心等待，不要重复发起。"""
    with _clean_stdout():
        try:
            return kb_rag.ask_text(query, top_k=max(1, min(int(top_k), 20)))
        except SystemExit as e:
            return f"[问答失败] {e}"
        except Exception as e:
            return f"[工具执行错误] {type(e).__name__}: {e}"


if __name__ == "__main__":
    # stdio 模式运行：阻塞在协议循环，直到客户端断开。
    # mcp.run 内部只向 sys.stdout（已指向干净的 PROTOCOL_FD）写 JSON-RPC。
    mcp.run(transport="stdio")
