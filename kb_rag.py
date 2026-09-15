#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
kb_rag.py —— 「项目代码助教」：本地 RAG 查询 CLI
================================================
读取知识库 Markdown 目录 → 分块 → Ollama 嵌入(bge-m3) → ChromaDB 持久化
→ 检索 Top-K → qwen3 基于检索结果总结半成品的进度与核心接口。

依赖（除 chromadb 外全部标准库，零 LangChain/LlamaIndex，轻量可审计）：
    pip install chromadb
前提：
    本机 Ollama 已运行且已拉取：
        ollama pull bge-m3        # 嵌入模型（1.1GB, 1024 维，中英双语）
        ollama pull qwen3:8b      # 对话模型

用法：
    # 1. 构建索引（只需在文档变化后重跑，增量更新）
    py kb_rag.py index ./kb-docs --collection kb-projects

    # 2. 查询
    py kb_rag.py ask "查询关于电机驱动的半成品代码" --collection kb-projects

    # 3. 纯检索（不调大模型，看命中了哪些块）
    py kb_rag.py search "电机驱动" --collection kb-projects

    # 4. 交互模式
    py kb_rag.py chat --collection kb-projects

设计说明（为什么不用 LangChain）：
    个人知识库场景链路短（读文件→切分→嵌入→存取→拼提示词），
    直接 200 行可读代码比引入框架依赖树更可控、排障更容易。
    若未来需要重排/多路召回/Agent，再平滑迁移 LlamaIndex 不迟。
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Iterable

# ---------------- 配置（按需修改） ----------------
# Chroma 目录用「脚本所在目录」锚定的绝对路径：
# 无论从哪个工作目录启动（CLI、VS Code MCP、cron），都指向同一份向量库
_SCRIPT_DIR = Path(__file__).resolve().parent
OLLAMA_HOST = "http://127.0.0.1:11434"   # Ollama API 地址
EMBED_MODEL = "bge-m3"                   # 嵌入模型（中英双语，1024 维）
CHAT_MODEL = "qwen3:8b"                  # 对话模型
CHROMA_DIR = str(_SCRIPT_DIR / "chroma_data")  # ChromaDB 持久化目录（绝对路径）
DEFAULT_KB_DIR = str(_SCRIPT_DIR / "kb-docs")  # 默认知识库目录（绝对路径）
CHUNK_SIZE = 800                         # 每块目标字符数（按标题切分后的软上限）
CHUNK_OVERLAP = 100                      # 相邻块重叠字符数
TOP_K = 5                                # 检索召回块数
MAX_CTX_CHARS = 6000                     # 送入大模型的上下文硬上限（防超 token）


def normalize_ollama_host() -> str:
    """读环境变量 OLLAMA_HOST 并规范化；返回最终 host。
    兼容只写 host 不写协议的写法；Windows 上 0.0.0.0 作为客户端目标不可达，替换为 127.0.0.1。"""
    global OLLAMA_HOST
    env = os.environ.get("OLLAMA_HOST", "").strip()
    if not env:
        return OLLAMA_HOST
    if not env.startswith(("http://", "https://")):
        env = "http://" + env
    env = env.rstrip("/")
    if re.search(r"://0\.0\.0\.0", env):
        env = env.replace("0.0.0.0", "127.0.0.1")
    OLLAMA_HOST = env
    return OLLAMA_HOST


# ================================================================
# ① Ollama HTTP 客户端（标准库实现，无 ollama-py 依赖）
# ================================================================

def ollama_post(path: str, payload: dict, timeout: int = 300) -> dict:
    """POST Ollama API 并返回 JSON；网络错误转成可读中文提示"""
    url = f"{OLLAMA_HOST}{path}"
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.URLError as e:
        raise SystemExit(
            f"[连接失败] 无法访问 Ollama（{url}）：{e.reason}\n"
            f"请确认：1) Ollama 已启动（`ollama serve` 或桌面端）；"
            f"2) 若在 Docker 网络内，把 OLLAMA_HOST 改为 http://ollama:11434"
        )
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")[:200]
        raise SystemExit(f"[API 错误] {path} → HTTP {e.code}：{body}")


def embed_texts(texts: list[str]) -> list[list[float]]:
    """批量嵌入。Ollama 的 /api/embed 支持批量 input，单块失败整批退化为逐条重试"""
    if not texts:
        return []
    r = ollama_post("/api/embed", {"model": EMBED_MODEL, "input": texts})
    embs = r.get("embeddings") or []
    if len(embs) == len(texts):
        return embs
    # 兜底：逐条嵌入（某条超长或含非法字符时不至于全军覆没）
    out = []
    for t in texts:
        r1 = ollama_post("/api/embed", {"model": EMBED_MODEL, "input": [t]})
        out.append((r1.get("embeddings") or [[0.0]])[0])
    return out


def chat_stream(prompt: str, system: str) -> str:
    """流式调用对话模型，边生成边打印；返回完整回答"""
    payload = {
        "model": CHAT_MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ],
        "stream": True,
        "options": {"temperature": 0.3},   # 复盘总结任务要稳定不要发散
    }
    req = urllib.request.Request(
        f"{OLLAMA_HOST}/api/chat",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    try:
        collected: list[str] = []
        with urllib.request.urlopen(req, timeout=600) as resp:
            for raw_line in resp:
                obj = json.loads(raw_line.decode("utf-8"))
                token = obj.get("message", {}).get("content", "")
                if token:
                    collected.append(token)
                    print(token, end="", flush=True)
                if obj.get("done"):
                    break
        print()
        return "".join(collected)
    except urllib.error.HTTPError as e:
        raise SystemExit(f"[对话失败] HTTP {e.code}：{e.read().decode('utf-8', 'replace')[:200]}")


# ================================================================
# ② Markdown 读取与分块（按标题结构切，保留块级元数据）
# ================================================================

SKIP_DIRS = {".git", "__pycache__", "node_modules", "venv", ".venv", "chroma_data"}


def iter_md_files(root: Path) -> Iterable[Path]:
    for p in sorted(root.rglob("*.md")):
        if p.is_file() and not any(part in SKIP_DIRS for part in p.parts):
            yield p


def split_markdown(md_text: str) -> list[str]:
    """
    结构感知分块：
      1) 先按 ##/### 标题切段（复盘文档天然分节：元数据/状态/架构/TODO）
      2) 段超过 CHUNK_SIZE 再按段落滑动窗口，带 CHUNK_OVERLAP 重叠
    每块自带标题上下文（拼回最近的 ## 标题），检索命中率显著高于裸滑窗。
    """
    # 剥离附录代码块中的超长原文，避免无意义重复（复盘文档附录是机器生成区）
    lines, sections, current_title = [], [], "（开头）"
    for line in md_text.splitlines():
        m = re.match(r"^(#{2,3})\s+(.*)", line)
        if m:
            if lines:
                sections.append((current_title, "\n".join(lines).strip()))
            lines = []
            current_title = m.group(2).strip()
            lines.append(line)
        else:
            lines.append(line)
    if lines:
        sections.append((current_title, "\n".join(lines).strip()))

    chunks: list[str] = []
    for title, body in sections:
        if not body:
            continue
        if len(body) <= CHUNK_SIZE:
            chunks.append(f"[{title}]\n{body}")
            continue
        # 超长段 → 段落累积 + 滑窗
        paras = [p for p in body.split("\n\n") if p.strip()]
        buf, buf_len = "", 0
        for para in paras:
            if buf_len + len(para) > CHUNK_SIZE and buf:
                chunks.append(f"[{title}]\n{buf}")
                buf = buf[-CHUNK_OVERLAP:] + "\n\n" + para  # 保留尾部做重叠
                buf_len = len(buf)
            else:
                buf = f"{buf}\n\n{para}" if buf else para
                buf_len = len(buf)
        if buf.strip():
            chunks.append(f"[{title}]\n{buf}")
    return [c for c in chunks if len(c) > 40]  # 过滤空壳块


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


# ================================================================
# ③ ChromaDB 索引（增量：内容没变的块不重复嵌入）
# ================================================================

def get_collection(collection: str):
    import chromadb  # 延迟导入，--help 等场景不必先装
    client = chromadb.PersistentClient(path=CHROMA_DIR)
    return client.get_or_create_collection(
        name=collection,
        metadata={"hnsw:space": "cosine"},  # 余弦距离，文本检索惯例
    )


def build_index(root: Path, collection_name: str) -> None:
    t0 = time.time()
    files = list(iter_md_files(root))
    if not files:
        raise SystemExit(f"[错误] {root} 下没有找到 .md 文件")

    col = get_collection(collection_name)
    # 已入库的块 id 集合（内容哈希后缀相同 = 内容没变 = 跳过）
    existing: set[str] = set(col.get().get("ids", []))

    # 两轮扫描：先算出「当前文件集合应产生的全部块 id」，
    # 库里任何不属于这个集合的 id 都是过期块（文件删除/重命名/内容变更后的旧 hash）。
    # upsert 只增不删，同前缀不同 hash 的旧块会永久残留污染检索——必须显式清理。
    current_ids: set[str] = set()
    file_texts: dict[str, str] = {}
    for f in files:
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            print(f"  [跳过] 读取失败 {f.name}: {e}")
            continue
        rel = str(f.relative_to(root))
        file_texts[rel] = text
        for i, chunk in enumerate(split_markdown(text)):
            current_ids.add(f"{rel}::c{i}::{content_hash(chunk)}")

    stale_ids = [i for i in existing if i not in current_ids]
    if stale_ids:
        col.delete(ids=stale_ids)
        print(f"  已清理 {len(stale_ids)} 个过期块（文件变更/删除后的旧内容残留）")

    total_chunks = len(current_ids)
    docs, metas, ids, embed_inputs = [], [], [], []
    for rel, text in file_texts.items():
        for i, chunk in enumerate(split_markdown(text)):
            cid = f"{rel}::c{i}::{content_hash(chunk)}"
            if cid in existing and cid not in stale_ids:
                continue  # 内容未变，跳过（增量索引的关键）
            docs.append(chunk)
            metas.append({
                "file": rel, "chunk_index": i,
                "source": Path(rel).name, "mtime": dt.date.today().isoformat(),
            })
            ids.append(cid)
            embed_inputs.append(chunk)

    if not embed_inputs:
        print(f"✅ 索引已是最新：{len(files)} 个文件 / {total_chunks} 块，无需更新")
        return
    print(f"发现 {len(files)} 个文件，{total_chunks} 块，其中 {len(embed_inputs)} 块需要（重新）嵌入…")
    B = 32  # 批大小：bge-m3 在 /api/embed 单批 32 条很稳
    for i in range(0, len(embed_inputs), B):
        batch_emb = embed_texts(embed_inputs[i:i + B])
        col.upsert(
            ids=ids[i:i + B],
            embeddings=batch_emb,
            documents=docs[i:i + B],
            metadatas=metas[i:i + B],
        )
        print(f"  已嵌入 {min(i + B, len(embed_inputs))}/{len(embed_inputs)}")
    new_chunks = len(embed_inputs)
    print(f"✅ 索引构建完成：新增 {new_chunks} 块，库内共 {col.count()} 块，"
          f"耗时 {time.time() - t0:.1f}s → {CHROMA_DIR}")


# ================================================================
# ④ 检索 + 助教提示词
# ================================================================

SYSTEM_PROMPT = """你是一位资深嵌入式与全栈项目的"代码助教"，专门帮开发者复盘自己的半成品项目。
规则：
1. 只依据提供的【检索资料】回答，资料里没有的信息明确说"文档中未记录"，禁止编造。
2. 回答结构固定为两段：【开发进度】该半成品目前完成了什么、缺什么、处于什么状态；
   【核心接口】列出资料中出现的关键模块/函数/类/引脚/协议，并说明其职责。
3. 资料来自检索，可能包含不相关的项目内容，请先筛选与问题相关的项目再总结。
4. 使用简体中文，条目化输出，引用来源时注明出自哪个文件。"""


def retrieve(query: str, collection_name: str, top_k: int = TOP_K) -> list[dict]:
    col = get_collection(collection_name)
    if col.count() == 0:
        raise SystemExit(f"[错误] 集合 {collection_name} 为空，请先运行 index 子命令")
    q_emb = embed_texts([query])[0]
    res = col.query(query_embeddings=[q_emb], n_results=min(top_k, col.count()))
    hits = []
    for doc, meta, dist in zip(res["documents"][0], res["metadatas"][0], res["distances"][0]):
        hits.append({"doc": doc, "file": meta["file"], "sim": 1 - dist})
    return hits


def format_context(hits: list[dict]) -> str:
    parts, used = [], 0
    for i, h in enumerate(hits, 1):
        piece = f"--- 资料{i}（出处：{h['file']}，相似度 {h['sim']:.2f}）---\n{h['doc']}"
        if used + len(piece) > MAX_CTX_CHARS:
            parts.append(f"--- 资料{i}（出处：{h['file']}，相似度 {h['sim']:.2f}）---\n（内容过长已截断）")
            break
        parts.append(piece)
        used += len(piece)
    return "\n\n".join(parts)


def do_ask(query: str, collection_name: str, top_k: int) -> None:
    print(f"🔍 检索「{query}」Top-{top_k} …")
    hits = retrieve(query, collection_name, top_k)
    print("命中文件：")
    seen = set()
    for h in hits:
        if h["file"] not in seen:
            seen.add(h["file"])
            print(f"  · {h['file']}（最高相似度 {h['sim']:.2f}）")
    print("\n" + "─" * 60 + "\n🤖 助教回答：\n")
    user_prompt = f"【检索资料】\n{format_context(hits)}\n\n【我的问题】\n{query}"
    chat_stream(user_prompt, SYSTEM_PROMPT)


def do_search(query: str, collection_name: str, top_k: int) -> None:
    hits = retrieve(query, collection_name, top_k)
    for i, h in enumerate(hits, 1):
        print(f"\n── #{i} {h['file']}（相似度 {h['sim']:.2f}）──")
        print(h["doc"][:400] + ("…" if len(h["doc"]) > 400 else ""))


# ================================================================
# ④-bis 编程接口层（供 mcp_server.py / 其他脚本 import 调用）
#     与 do_ask/do_search 的区别：不 print、不调 SystemExit，返回字符串
# ================================================================

def search_text(query: str, collection_name: str = "kb-projects", top_k: int = TOP_K) -> str:
    """纯检索：返回格式化的命中列表（文件、相似度、片段），不调用对话模型"""
    try:
        hits = retrieve(query, collection_name, top_k)
    except SystemExit as e:
        return f"[检索失败] {e}"
    if not hits:
        return "[无结果] 向量库中未检索到相关内容（先运行 index 命令建库？）"
    out = [f"检索「{query}」命中 {len(hits)} 块："]
    for i, h in enumerate(hits, 1):
        out.append(f"\n#{i} {h['file']}（相似度 {h['sim']:.2f}）\n{h['doc'][:500]}")
    return "\n".join(out)


def ask_text(query: str, collection_name: str = "kb-projects", top_k: int = TOP_K) -> str:
    """检索 + 本地大模型总结：返回完整回答文本（流式打印关闭，供程序化消费）"""
    global _STREAM_TO_STDOUT
    try:
        hits = retrieve(query, collection_name, top_k)
    except SystemExit as e:
        return f"[检索失败] {e}"
    if not hits:
        return "[无结果] 向量库为空或未命中，无法回答。"
    ctx = format_context(hits)
    files = sorted({h["file"] for h in hits})
    user_prompt = f"【检索资料】\n{ctx}\n\n【我的问题】\n{query}"
    # 流式打印对 MCP stdio 是致命的（会污染协议管道），改为静默收集
    answer = chat_collect(user_prompt, SYSTEM_PROMPT)
    header = f"参考来源：{', '.join(files)}\n{'─' * 40}\n"
    return header + answer


def chat_collect(prompt: str, system: str) -> str:
    """chat_stream 的静默版本：不打印，只返回完整回答（MCP/编程调用专用）"""
    payload = {
        "model": CHAT_MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ],
        "stream": True,
        "options": {"temperature": 0.3},
    }
    req = urllib.request.Request(
        f"{OLLAMA_HOST}/api/chat",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    try:
        collected: list[str] = []
        with urllib.request.urlopen(req, timeout=600) as resp:
            for raw_line in resp:
                obj = json.loads(raw_line.decode("utf-8"))
                token = obj.get("message", {}).get("content", "")
                if token:
                    collected.append(token)
                if obj.get("done"):
                    break
        return "".join(collected)
    except urllib.error.HTTPError as e:
        return f"[对话失败] HTTP {e.code}：{e.read().decode('utf-8', 'replace')[:200]}"
    except urllib.error.URLError as e:
        return f"[连接失败] 无法访问 Ollama（{OLLAMA_HOST}）：{e}"


# ================================================================
# ⑤ CLI
# ================================================================

def main() -> int:
    ap = argparse.ArgumentParser(description="项目代码助教 —— 本地 RAG 查询 CLI")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_index = sub.add_parser("index", help="构建/更新向量索引")
    p_index.add_argument("root", help="Markdown 知识库目录")
    p_index.add_argument("--collection", default="kb-projects", help="Chroma 集合名")

    p_ask = sub.add_parser("ask", help="提问（检索 + 大模型总结）")
    p_ask.add_argument("query", help="例如：查询关于电机驱动的半成品代码")
    p_ask.add_argument("--collection", default="kb-projects")
    p_ask.add_argument("--top-k", type=int, default=TOP_K)

    p_search = sub.add_parser("search", help="纯向量检索（不调大模型）")
    p_search.add_argument("query")
    p_search.add_argument("--collection", default="kb-projects")
    p_search.add_argument("--top-k", type=int, default=TOP_K)

    p_chat = sub.add_parser("chat", help="交互式问答（exit 退出）")
    p_chat.add_argument("--collection", default="kb-projects")
    p_chat.add_argument("--top-k", type=int, default=TOP_K)

    # 允许用环境变量覆盖默认 host（Docker 部署场景）；
    # 兼容只写 host 不写协议的写法（如 OLLAMA_HOST=0.0.0.0 或 localhost:11434）
    import os
    global OLLAMA_HOST
    env_host = os.environ.get("OLLAMA_HOST", "").strip()
    if env_host:
        if not env_host.startswith(("http://", "https://")):
            env_host = "http://" + env_host
        OLLAMA_HOST = env_host.rstrip("/")

    args = ap.parse_args()
    if args.cmd == "index":
        build_index(Path(args.root).expanduser().resolve(), args.collection)
    elif args.cmd == "ask":
        do_ask(args.query, args.collection, args.top_k)
    elif args.cmd == "search":
        do_search(args.query, args.collection, args.top_k)
    elif args.cmd == "chat":
        print("项目代码助教 · 交互模式（输入 exit 退出）")
        while True:
            try:
                q = input("\n❓ ").strip()
            except (EOFError, KeyboardInterrupt):
                break
            if not q or q.lower() in {"exit", "quit", "q"}:
                break
            do_ask(q, args.collection, args.top_k)
    return 0


if __name__ == "__main__":
    sys.exit(main())
