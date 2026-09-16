#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
kb_dashboard.py —— 本地项目看板（零第三方依赖，仅标准库）

用途：不依赖 Docker，直接用浏览器审阅 kb-docs/ 的项目状态，
     并内置基于本地 Ollama + ChromaDB 的问答框。

启动：
    py kb_dashboard.py                    # 默认 http://127.0.0.1:8765
    py kb_dashboard.py --port 9000        # 换端口
    py kb_dashboard.py --no-ai            # 关闭 AI 问答（纯静态看板）

设计说明：
  · 用 http.server（标准库）提供服务，不需要 Flask/FastAPI
  · AI 问答直接调用 kb_rag 的检索与大模型，与本机原生 Ollama 对接
  · 只绑定 127.0.0.1，局域网与公网均不可达
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import threading
import urllib.parse
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
KB_DOCS = SCRIPT_DIR / "kb-docs"
sys.path.insert(0, str(SCRIPT_DIR))

ENABLE_AI = True
CORS_ORIGIN = "*"


# ---------------------------------------------------------------
# 数据层：解析 kb-docs/*.md 提取项目卡片信息
# ---------------------------------------------------------------

def parse_project(md_path: Path) -> dict:
    """从复盘文档提取看板需要的字段（容错解析，缺失即留空）"""
    text = md_path.read_text(encoding="utf-8", errors="replace")
    info = {
        "file": md_path.name,
        "title": md_path.stem,
        "status": "未填写",
        "usability": "",
        "summary": "",
        "source": "",
        "tech": "",
        "last_active": "",
        "done": 0,
        "missing": 0,
        "todos": [],
        "raw_size": len(text),
    }

    # 标题（# 项目名 —— ...）
    m = re.search(r"^#\s+(.+?)\s+——", text, re.M)
    if m:
        info["title"] = m.group(1).strip()

    # §2.1 整体状态（占位符 → 显示为"未填写"）
    m = re.search(r"^- \*\*整体状态\*\*：(.+)$", text, re.M)
    if m:
        raw = m.group(1).strip()
        if raw.startswith("{{"):
            info["status"] = "未填写"
        else:
            # 去掉"（模型判定…）"等括注，并按句号截断（手写文档可能很长）
            s = re.split(r"[（(]", raw)[0].strip()
            s = re.split(r"[。；;]", s)[0].strip()
            info["status"] = s[:16]

    # §2.1 可用性
    m = re.search(r"^- \*\*可用性\*\*：(.+)$", text, re.M)
    if m:
        info["usability"] = m.group(1).strip()[:120]

    # 一句话定位
    m = re.search(r"^>\s*一句话定位：(.+)$", text, re.M)
    if m:
        s = m.group(1).strip()
        info["summary"] = "" if s.startswith("{{") else s[:100]

    # 附录：来源扫描根 / 检测到的语言 / 最后活跃
    m = re.search(r"^来源扫描根：(.+)$", text, re.M)
    if m:
        info["source"] = Path(m.group(1).strip()).name
    m = re.search(r"^检测到的语言：(.+)$", text, re.M)
    if m:
        info["tech"] = m.group(1).strip()
    m = re.search(r"^最后活跃（文件 mtime）：(.+)$", text, re.M)
    if m:
        info["last_active"] = m.group(1).strip()

    # 模块计数：表格中非占位符的数据行
    sec = re.search(r"### 2\.2 已完成模块(.+?)(?=###|\Z)", text, re.S)
    if sec:
        info["done"] = len([l for l in sec.group(1).splitlines()
                            if l.strip().startswith("|") and "{{" not in l and "---" not in l])
    sec = re.search(r"### 2\.3 缺失/损坏模块(.+?)(?=###|\Z)", text, re.S)
    if sec:
        info["missing"] = len([l for l in sec.group(1).splitlines()
                               if l.strip().startswith("|") and "{{" not in l and "---" not in l])

    # TODO 清单（§4.1）
    sec = re.search(r"### 4\.1 TODO List(.+?)(?=###|\Z)", text, re.S)
    if sec:
        for line in sec.group(1).splitlines():
            line = line.strip()
            if line.startswith("- [ ]") or line.startswith("- [x]"):
                task = line[5:].strip()
                if "{{" not in task:
                    info["todos"].append({"done": line.startswith("- [x]"), "text": task[:110]})
    return info


def load_projects() -> list[dict]:
    if not KB_DOCS.is_dir():
        return []
    projects = []
    for md in sorted(KB_DOCS.glob("*.md")):
        if md.name == "INDEX.md":
            continue
        try:
            projects.append(parse_project(md))
        except Exception:
            continue
    # 排序：有状态的优先，其次按最后活跃倒序
    projects.sort(key=lambda p: (p["status"] == "未填写", p["last_active"]), reverse=False)
    return projects


def projects_api_payload() -> dict:
    """返回稳定的公开 API 结构，避免前端依赖看板内部字段。"""
    projects = []
    for item in load_projects():
        tech = item["tech"]
        if isinstance(tech, str):
            tech = [value.strip() for value in re.split(r"[,，/|·]+", tech) if value.strip()]
        projects.append({
            "name": item["title"],
            "status": item["status"],
            "updated_at": item["last_active"],
            "summary": item["summary"],
            "usability": item["usability"],
            "source": item["source"],
            "tech": tech,
            "todos": item["todos"],
            "metrics": {
                "done": item["done"],
                "missing": item["missing"],
                "pending_todos": sum(not todo["done"] for todo in item["todos"]),
            },
        })
    return {
        "projects": projects,
        "count": len(projects),
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }


# ---------------------------------------------------------------
# AI 层：调用 kb_rag（检索 + 本地大模型）
# ---------------------------------------------------------------

def ai_answer(question: str, top_k: int = 4) -> str:
    if not ENABLE_AI:
        return "（AI 问答已关闭，启动时去掉 --no-ai 可启用）"
    try:
        import kb_rag
        _force_local_ollama()
        return kb_rag.ask_text(question, top_k=top_k)
    except Exception as e:
        return f"[AI 调用失败] {type(e).__name__}: {e}"


def ai_search(question: str, top_k: int = 4) -> str:
    try:
        import kb_rag
        _force_local_ollama()
        return kb_rag.search_text(question, top_k=top_k)
    except Exception as e:
        return f"[检索失败] {type(e).__name__}: {e}"


def _force_local_ollama() -> None:
    """确保走本机标准端口。

    系统环境变量 OLLAMA_HOST=0.0.0.0 会覆盖默认值，而 0.0.0.0 作为客户端
    目标地址无效（缺端口号），导致连接被拒。这里强制回落到 127.0.0.1:11434。
    """
    import os
    raw = os.environ.get("OLLAMA_HOST", "").strip()
    # 只要没写端口，或写成 0.0.0.0，就强制用本机标准地址
    if not raw or ":" not in raw.replace("http://", "").replace("https://", "") \
            or "0.0.0.0" in raw:
        os.environ["OLLAMA_HOST"] = "127.0.0.1:11434"
    import kb_rag
    kb_rag.normalize_ollama_host()


# ---------------------------------------------------------------
# 视图层：单页 HTML（内嵌 CSS/JS，无外部依赖，离线可用）
# ---------------------------------------------------------------

PAGE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>项目看板 · KB-Stack</title>
<style>
  * { box-sizing: border-box; }
  body { margin:0; font-family: -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif;
         background:#0f1115; color:#e6e8eb; line-height:1.6; }
  header { padding:22px 28px; border-bottom:1px solid #242832; background:#14171d;
           display:flex; align-items:center; justify-content:space-between; flex-wrap:wrap; gap:12px; }
  h1 { margin:0; font-size:19px; font-weight:600; letter-spacing:.3px; }
  h1 span { color:#6b7280; font-weight:400; font-size:13px; margin-left:10px; }
  .stats { display:flex; gap:18px; font-size:13px; color:#9ca3af; }
  .stats b { color:#e6e8eb; font-size:15px; }
  main { padding:24px 28px 60px; max-width:1400px; margin:0 auto; }
  .grid { display:grid; grid-template-columns:repeat(auto-fill, minmax(390px,1fr)); gap:16px; }
  .card { background:#171a21; border:1px solid #242832; border-radius:10px; padding:16px 18px;
          transition:border-color .15s, transform .15s; }
  .card:hover { border-color:#3b4252; transform:translateY(-2px); }
  .card h3 { margin:0 0 4px; font-size:15px; font-weight:600; display:flex;
             align-items:center; justify-content:space-between; gap:8px; }
  .badge { font-size:11px; padding:2px 9px; border-radius:20px; white-space:nowrap; font-weight:500; }
  .b-done   { background:#14361f; color:#4ade80; border:1px solid #1e4d2b; }
  .b-active { background:#1a2f4a; color:#60a5fa; border:1px solid #24456b; }
  .b-shelf  { background:#3a2a12; color:#fbbf24; border:1px solid #553d1a; }
  .b-none   { background:#26292f; color:#9ca3af; border:1px solid #33373f; }
  .meta { font-size:12px; color:#6b7280; margin:6px 0 10px; display:flex; gap:12px; flex-wrap:wrap; }
  .desc { font-size:13px; color:#b8bdc7; margin:8px 0; min-height:20px; }
  .counts { display:flex; gap:14px; font-size:12px; margin-top:10px; padding-top:10px;
            border-top:1px solid #242832; }
  .counts span { color:#9ca3af; }
  .counts b { color:#e6e8eb; }
  .todos { margin-top:10px; font-size:12.5px; color:#9ca3af; }
  .todos div { padding:2px 0; }
  .todos .ok { color:#4ade80; text-decoration:line-through; opacity:.7; }
  .empty { color:#6b7280; text-align:center; padding:60px; }
  /* AI 面板 */
  .ai { position:fixed; right:22px; bottom:22px; width:430px; max-width:calc(100vw - 44px);
        background:#171a21; border:1px solid #2d3340; border-radius:12px;
        box-shadow:0 12px 40px rgba(0,0,0,.55); overflow:hidden; z-index:50; }
  .ai-head { padding:11px 15px; background:#1d2129; font-size:13.5px; font-weight:600;
             display:flex; justify-content:space-between; align-items:center; cursor:pointer; }
  .ai-head small { color:#6b7280; font-weight:400; }
  .ai-body { padding:13px 15px; display:none; }
  .ai.open .ai-body { display:block; }
  .ai-row { display:flex; gap:8px; }
  input[type=text] { flex:1; padding:9px 11px; border-radius:7px; border:1px solid #333944;
                     background:#0f1115; color:#e6e8eb; font-size:13px; font-family:inherit; }
  input[type=text]:focus { outline:none; border-color:#4b5768; }
  button { padding:9px 14px; border-radius:7px; border:none; cursor:pointer;
           background:#2563eb; color:#fff; font-size:13px; font-weight:500; font-family:inherit; }
  button:hover { background:#1d4ed8; }
  button.ghost { background:#262b33; }
  button.ghost:hover { background:#333942; }
  #ai-out { margin-top:12px; max-height:340px; overflow-y:auto; font-size:13px;
            white-space:pre-wrap; color:#b8bdc7; }
  #ai-out:empty { display:none; }
  .hint { font-size:11.5px; color:#6b7280; margin-top:8px; }
</style>
</head>
<body>
<header>
  <h1>项目看板 <span>KB-Stack · 本地知识库</span></h1>
  <div class="stats" id="stats"></div>
</header>
<main>
  <div class="grid" id="grid"></div>
</main>

<div class="ai open" id="ai">
  <div class="ai-head" onclick="document.getElementById('ai').classList.toggle('open')">
    <span>🤖 项目助教（本地 qwen3:8b）</span><small>点击折叠</small>
  </div>
  <div class="ai-body">
    <div class="ai-row">
      <input type="text" id="q" placeholder="例如：汇总所有项目的状态和搁置原因"
             onkeydown="if(event.key==='Enter')ask()">
      <button onclick="ask()">提问</button>
      <button class="ghost" onclick="searchOnly()">仅检索</button>
    </div>
    <div class="hint">AI 回答基于本地向量库检索 + 本地大模型生成，需 10-60 秒</div>
    <div id="ai-out"></div>
  </div>
</div>

<script>
const PROJECTS = __DATA__;
// 状态 → 徽章样式映射。【精确优先 + 长键在前】：
// "半成品-活跃"含"成品"二字，若"成品"排在前面做包含匹配会误染绿色
const BADGE = { '半成品-活跃':'b-active', '半成品-搁置':'b-shelf', '想法验证':'b-shelf',
                '已废弃':'b-none', '未填写':'b-none', '半成品':'b-active',
                '成品':'b-done', '可演示':'b-done' };

function badgeClass(s) {
  if (BADGE[s]) return BADGE[s];                 // 精确命中
  const keys = Object.keys(BADGE).sort((a, b) => b.length - a.length);
  for (const k of keys) if (s.indexOf(k) >= 0) return BADGE[k];  // 长键优先
  return 'b-none';
}
function esc(s) {
  return String(s).replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
}
function render() {
  const grid = document.getElementById('grid');
  if (!PROJECTS.length) { grid.innerHTML = '<div class="empty">kb-docs/ 下没有文档</div>'; return; }
  grid.innerHTML = PROJECTS.map(p => {
    const todos = (p.todos || []).slice(0, 4).map(t =>
      `<div class="${t.done ? 'ok' : ''}">${t.done ? '☑' : '☐'} ${esc(t.text)}</div>`).join('');
    return `<div class="card">
      <h3>${esc(p.title)}<span class="badge ${badgeClass(p.status)}">${esc(p.status)}</span></h3>
      <div class="meta">
        ${p.source ? `<span>📁 ${esc(p.source)}</span>` : ''}
        ${p.tech ? `<span>🔧 ${esc(p.tech)}</span>` : ''}
        ${p.last_active ? `<span>🕒 ${esc(p.last_active)}</span>` : ''}
      </div>
      <div class="desc">${esc(p.usability || p.summary || '（状态待填写）')}</div>
      ${todos ? `<div class="todos">${todos}</div>` : ''}
      <div class="counts">
        <span>已完成 <b>${p.done}</b></span>
        <span>缺失 <b>${p.missing}</b></span>
        <span>待办 <b>${(p.todos||[]).filter(t=>!t.done).length}</b></span>
      </div>
    </div>`;
  }).join('');

  const total = PROJECTS.length;
  const filled = PROJECTS.filter(p => p.status !== '未填写').length;
  const active = PROJECTS.filter(p => p.status.indexOf('活跃') >= 0 || /^(成品|可演示)/.test(p.status)).length;
  const pending = PROJECTS.reduce((n, p) => n + (p.todos||[]).filter(t=>!t.done).length, 0);
  document.getElementById('stats').innerHTML =
    `项目 <b>${total}</b> ｜ 已评估 <b>${filled}</b> ｜ 活跃 <b>${active}</b> ｜ 待办 <b>${pending}</b>`;
}
function show(text) {
  document.getElementById('ai-out').textContent = text;
}
async function callApi(mode) {
  const q = document.getElementById('q').value.trim();
  if (!q) return;
  show(mode === 'ask' ? '⏳ 检索 + 本地模型生成中，请稍候…' : '⏳ 检索中…');
  try {
    const r = await fetch('/api/' + mode, {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({ q: q })
    });
    show(await r.text());
  } catch (e) { show('请求失败: ' + e); }
}
function ask() { callApi('ask'); }
function searchOnly() { callApi('search'); }
render();
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, body: bytes, ctype: str, *, cors: bool = False) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if cors:
            self.send_header("Access-Control-Allow-Origin", CORS_ORIGIN)
            self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            if CORS_ORIGIN != "*":
                self.send_header("Vary", "Origin")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self) -> None:
        path = urllib.parse.urlparse(self.path).path
        if path != "/api/projects":
            self._send(404, b"not found", "text/plain; charset=utf-8")
            return
        self._send(204, b"", "text/plain; charset=utf-8", cors=True)

    def do_GET(self) -> None:
        path = urllib.parse.urlparse(self.path).path
        if path in ("/", "/index.html"):
            data = PAGE.replace("__DATA__", json.dumps(load_projects(), ensure_ascii=False))
            self._send(200, data.encode("utf-8"), "text/html; charset=utf-8")
        elif path == "/api/projects":
            body = json.dumps(projects_api_payload(), ensure_ascii=False).encode("utf-8")
            self._send(200, body, "application/json; charset=utf-8", cors=True)
        else:
            self._send(404, b"not found", "text/plain; charset=utf-8")

    def do_POST(self) -> None:
        try:
            n = int(self.headers.get("Content-Length", 0))
            payload = json.loads(self.rfile.read(n).decode("utf-8"))
            q = str(payload.get("q", "")).strip()
        except Exception:
            self._send(400, b"bad request", "text/plain; charset=utf-8")
            return
        path = urllib.parse.urlparse(self.path).path
        if not q:
            self._send(200, "（请输入问题）".encode("utf-8"), "text/plain; charset=utf-8")
            return
        if path == "/api/ask":
            result = ai_answer(q)
        elif path == "/api/search":
            result = ai_search(q)
        else:
            self._send(404, b"not found", "text/plain; charset=utf-8")
            return
        self._send(200, result.encode("utf-8"), "text/plain; charset=utf-8")

    def log_message(self, fmt: str, *args) -> None:
        pass  # 静默访问日志，保持终端干净


def main() -> int:
    global CORS_ORIGIN, ENABLE_AI
    ap = argparse.ArgumentParser(description="本地项目看板（零依赖）")
    ap.add_argument("--port", type=int, default=8765, help="端口（默认 8765）")
    ap.add_argument("--host", default="127.0.0.1", help="绑定地址（默认仅本机回环）")
    ap.add_argument(
        "--cors-origin",
        default=os.environ.get("KB_DASHBOARD_CORS_ORIGIN", "*"),
        help="允许跨域访问 API 的前端源（默认 *）",
    )
    ap.add_argument("--no-ai", action="store_true", help="关闭 AI 问答（纯静态看板）")
    args = ap.parse_args()
    ENABLE_AI = not args.no_ai
    CORS_ORIGIN = args.cors_origin.strip() or "*"

    if not KB_DOCS.is_dir():
        print(f"[错误] 未找到 kb-docs 目录：{KB_DOCS}")
        return 1

    projects = load_projects()
    print(f"📊 已加载 {len(projects)} 个项目文档")
    for p in projects[:5]:
        print(f"   · {p['title']} [{p['status']}]")
    if len(projects) > 5:
        print(f"   … 其余 {len(projects) - 5} 个")

    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    url = f"http://{args.host}:{args.port}"
    print(f"\n✅ 看板已启动：{url}")
    print(f"   AI 问答：{'开启（需本地 Ollama）' if ENABLE_AI else '关闭'}")
    print(f"   API CORS：{CORS_ORIGIN}")
    print("   按 Ctrl+C 停止\n")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    return 0


if __name__ == "__main__":
    sys.exit(main())
