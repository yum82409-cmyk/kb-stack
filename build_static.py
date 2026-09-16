#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
build_static.py —— 把项目看板构建为纯静态站点（零后端部署）
================================================================
用途：静态托管平台（Vercel / Netlify / Cloudflare Pages / GitHub Pages）
     无法运行 Python 常驻进程和 Ollama，本脚本在【构建时】把 kb-docs/
     的数据全部烘进 HTML —— 产物是单个 index.html，无任何运行时依赖。

用法：
    py build_static.py                    # 输出到 ./dist/
    py build_static.py -o ./public        # 指定输出目录（Netlify 惯例是 public）

设计：
  · 复用 kb_dashboard.py 的解析逻辑（parse_project / load_projects），
    保证线上看板与本机看板数据口径一致
  · AI 问答面板自动隐藏（静态站无后端；AI 继续用本机 http://127.0.0.1:8765）
  · 生成后整个 dist/ 目录可直接拖到任何静态托管

注意（隐私）：静态站是公开的。kb-docs/ 里的复盘文档会以卡片形式
     呈现在公网上。构建前请确认文档里没有你不想公开的内容
     （脚本会扫描常见敏感标记并提醒）。
"""

from __future__ import annotations

import argparse
import re
import shutil
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from kb_dashboard import load_projects  # 复用解析逻辑（单一数据源）

# 构建：页面骨架直接复用 kb_dashboard 的 PAGE 常量，再打两个静态补丁
from kb_dashboard import PAGE


# 静态版补丁：1) 移除 AI 面板（无后端） 2) 顶部标注构建时间
STATIC_PATCH_JS = """
/* —— 静态构建版：隐藏 AI 面板 —— */
(function(){
  const ai = document.getElementById('ai');
  if (ai) ai.style.display = 'none';
})();
"""

SENSITIVE_MARKS = [
    (re.compile(r"[A-Z]:\\Users\\[^\s\"']+"), "本机用户路径"),
    (re.compile(r"\beyJhIjoi[A-Za-z0-9]{20,}"), "疑似 Cloudflare Token"),
    (re.compile(r"\btskey-auth-[A-Za-z0-9]{15,}"), "疑似 Tailscale 密钥"),
    (re.compile(r"\bsk-[A-Za-z0-9]{20,}"), "疑似 OpenAI 风格密钥"),
]


def privacy_scan(projects: list[dict]) -> list[str]:
    """扫已解析字段的敏感残留（卡片上会公开的内容）"""
    issues = []
    for p in projects:
        blob = " ".join(str(v) for v in p.values() if isinstance(v, str))
        for pat, label in SENSITIVE_MARKS:
            if pat.search(blob):
                issues.append(f"{p['title']}: 含{label}")
    return issues


def build(out_dir: Path) -> Path:
    projects = load_projects()
    if not projects:
        raise SystemExit("[错误] kb-docs/ 下没有可解析的文档")

    # 隐私扫描（只警告不阻断——留给人判断）
    issues = privacy_scan(projects)
    if issues:
        print("⚠️  隐私扫描发现以下内容将【公开】到网站上：")
        for i in issues:
            print(f"   - {i}")
        print("   如需移除，请先编辑 kb-docs/ 对应文档再重新构建\n")

    data = "[\n" + ",\n".join(
        "  " + __import__("json").dumps(p, ensure_ascii=False) for p in projects
    ) + "\n]"

    html = PAGE.replace("__DATA__", data)
    # 注入静态补丁（</body> 前执行，覆盖动态版的 AI 面板）
    html = html.replace("</body>", f"<script>{STATIC_PATCH_JS}</script>\n</body>")

    # 页脚：构建时间与项目数（放在 <h1> 的 span 里追加）
    import datetime as dt
    stamp = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    html = html.replace(
        "<h1>项目看板 <span>KB-Stack · 本地知识库</span></h1>",
        f"<h1>项目看板 <span>KB-Stack · 构建于 {stamp} · {len(projects)} 个项目</span></h1>",
    )
    # 标题同步
    html = html.replace("<title>项目看板 · KB-Stack</title>",
                        "<title>KB-Stack · 项目看板</title>")

    out_dir.mkdir(parents=True, exist_ok=True)
    # 清空输出目录（只清一层，避免误删）
    for old in out_dir.iterdir():
        if old.is_file():
            old.unlink()
        elif old.is_dir():
            shutil.rmtree(old)
    (out_dir / "index.html").write_text(html, encoding="utf-8")

    size_kb = (out_dir / "index.html").stat().st_size / 1024
    print(f"✅ 静态站点已构建：{out_dir / 'index.html'}（{size_kb:.1f} KB）")
    print(f"   项目数：{len(projects)}（含 {sum(1 for p in projects if p['status'] != '未填写')} 个已评估）")
    print(f"   AI 面板：已隐藏（静态站无后端；AI 问答继续用本机 kb_dashboard.py）")
    print(f"\n部署预览：直接用浏览器打开 {out_dir / 'index.html'} 即可检查效果")
    return out_dir


def main() -> int:
    ap = argparse.ArgumentParser(description="构建纯静态项目看板")
    ap.add_argument("-o", "--out", default="./dist", help="输出目录（默认 ./dist）")
    args = ap.parse_args()
    out = Path(args.out).expanduser().resolve()
    build(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
