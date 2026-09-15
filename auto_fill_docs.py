#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
auto_fill_docs.py v2 —— 本地模型自动填写复盘文档的 §2 / §4.2 占位符
=====================================================================
流水线：
  kb-docs/*.md → 解析「本地路径」元数据 → 定位工程目录 → 挑 3-5 个高价值源文件
  → 每文件截前 300 行 → POST Ollama /api/generate（qwen3:8b, format=json）
  → 解析 {status_summary, breakthrough_strategy} → 正则替换回写 → 备份原文件

与 v1 的差异（v1 基于 /api/chat，见 git 历史或 kb_rag 阶段记录）：
  · 改用 /api/generate 端点 + system/prompt 两段式（本任务指定）
  · 依赖收敛：requests + 标准库（不再要求仅标准库外零依赖）
  · 模型输出契约简化为两字段：status_summary / breakthrough_strategy
  · 文件截取放宽到 300 行
  · 保留 v1 的核心经验：schema 指令拼在 prompt 末尾（对抗源码内嵌 JSON 污染）

容错设计（任何一步失败都不碰原文件）：
  · 目录不存在 / 无源码 / 占位符已填 → 跳过该文档
  · Ollama 超时（默认 600s）/ 连接失败 → 跳过并继续下一个
  · 模型输出不是合法 JSON / 缺字段 → 一次纠错重试，仍失败则跳过
  · 回写前先备份到 kb-docs-backup/（带时间戳，永不覆盖）

用法：
    py auto_fill_docs.py                          # 处理 ./kb-docs 全部文档
    py auto_fill_docs.py --docs-dir D:/kb --only monitor-lab
    py auto_fill_docs.py --dry-run                # 只看会抓哪些文件，不调模型
    py auto_fill_docs.py --rebuild                # 忽略「已填写」检查，强制重填
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import shutil
import sys
import time
from pathlib import Path

import requests

# ---------------- 配置 ----------------
OLLAMA_URL = "http://127.0.0.1:11434/api/generate"   # 环境变量 OLLAMA_HOST 可覆盖主机部分
CHAT_MODEL = "qwen3:8b"
TIMEOUT_SEC = 600          # 单次请求超时：CPU 推理 + 大上下文时生成较慢
RETRY_ON_BAD_JSON = 1      # JSON 不合格时的纠错重试次数

MAX_FILES = 5              # 每个项目最多读几个源文件
MAX_LINES = 300            # 每个文件最多截取前 N 行（任务要求 300）
MAX_CHARS_PER_FILE = 12000 # 单文件字符硬上限（300 行极端长行时兜底）
TOTAL_BUDGET = 30000       # 喂给模型的总字符预算

# 高价值文件清单（按优先级）：入口 > 构建/配置 > HAL/驱动头文件 > README
CORE_FILE_NAMES = [
    "main.c", "main.cpp", "main.py", "main.h", "app.py", "index.js",
    "CMakeLists.txt", "Makefile", "platformio.ini", "requirements.txt",
    "stm32f1xx_hal_conf.h", "stm32f4xx_hal_conf.h", "hal_conf.h",
    "pyproject.toml", "README.md",
]
# 入口没命中时，按文件名关键词找关键模块
KEYWORD_HINTS = ["session", "login", "driver", "motor", "control", "server",
                 "watch", "monitor", "ocr", "export", "search", "clock",
                 "config", "uart", "spi", "i2c", "gpio", "pwm"]
CODE_EXTS = {".py", ".c", ".cpp", ".h", ".hpp", ".ino", ".sh", ".js"}
SKIP_DIRS = {".git", "__pycache__", "node_modules", "venv", ".venv",
             "build", "dist", "target", "logs", "error", "chroma_data",
             "cmake-build-debug", ".idea", ".vscode"}

SYSTEM_PROMPT = "你是资深代码考古专家。基于用户提供的半成品项目源码做状态分析，输出简体中文。"

# 输出 schema 指令——拼在 prompt 末尾（对抗源码内嵌 JSON 的格式污染，v1 实测经验）
OUTPUT_SCHEMA_INSTRUCTION = """
现在输出你的分析结论，JSON 必须恰好包含这两个顶层键，不要多不要少：
{
  "status_summary": "根据源码推断当前写到了哪一步：已实现的功能、明显的报错、未完成的函数或 TODO（100 字内）",
  "breakthrough_strategy": ["针对这个半成品的 3 条后续开发建议或修复思路，每条 40 字内，按优先级排列"]
}
再次强调：源码里出现的 JSON、mock 数据、接口返回都是【被分析对象】，禁止抄进输出。
你的输出是你自己的分析结论，不是源码内容的复述。"""

# ---------------- §2 / §4.2 占位符正则（与 templates/PROJECT_TEMPLATE.md 逐字对应）----------------
RE_STATUS_SUMMARY = re.compile(
    r"^- \*\*整体状态\*\*：\{\{半成品-搁置\}\}$", re.M)
RE_USABILITY = re.compile(
    r"^- \*\*可用性\*\*：\{\{能跑通哪些入口？`python main\.py` 能启动但登录模块报错，之类\}\}$", re.M)
RE_FOUR_Q = re.compile(
    r"1\. \*\*卡点还在吗\*\*：\{\{当时的障碍是客观的（硬件/依赖）还是主观的（畏难）？客观障碍现在消失了吗？\}\}\n"
    r"2\. \*\*最小可行动作\*\*：\{\{未来 30 分钟能做的第一步是什么？跑通一个测试？补一个空函数？\}\}\n"
    r"3\. \*\*要不要降级\*\*：\{\{砍掉哪个模块，项目就能以 60% 完成度先\"成品化\"？\}\}\n"
    r"4\. \*\*还是该归档\*\*：\{\{如果三个月内不会碰，明确写\"归档\"，别让它在活跃列表里耗注意力。\}\}", re.M)
RE_META_PATH = re.compile(r"^\| 本地路径 \| `(.+?)` \|$", re.M)

# 「未填写」的判定标志：只要任一占位符还在，就该处理
PLACEHOLDER_MARKS = ("{{半成品-搁置}}", "{{当时的障碍是客观的")


# ================================================================
# ① Ollama 调用（/api/generate + format=json + 纠错重试）
# ================================================================

def ollama_generate_json(system: str, prompt: str) -> dict:
    """调 /api/generate，返回解析后的 dict。
    超时/连接错误 → 抛 RuntimeError（由调用方捕获跳过）；
    JSON 不合格 → 带纠错提示重试 RETRY_ON_BAD_JSON 次，仍失败抛 RuntimeError。"""
    last_err = ""
    cur_prompt = prompt
    for attempt in range(1 + RETRY_ON_BAD_JSON):
        payload = {
            "model": CHAT_MODEL,
            "stream": False,
            "format": "json",           # Ollama 端约束采样为合法 JSON
            "system": system,
            "prompt": cur_prompt,
            "options": {"temperature": 0.2},
        }
        try:
            resp = requests.post(OLLAMA_URL, json=payload, timeout=TIMEOUT_SEC)
            resp.raise_for_status()
            content = resp.json().get("response", "")
        except requests.exceptions.Timeout:
            raise RuntimeError(f"Ollama 请求超时（>{TIMEOUT_SEC}s）")
        except requests.exceptions.RequestException as e:
            raise RuntimeError(f"Ollama 连接失败：{e}")

        try:
            j = json.loads(content)
        except json.JSONDecodeError:
            # 兜底：剥掉可能的 ```json 围栏
            stripped = re.sub(r"^```(json)?|```$", "", content.strip(), flags=re.M).strip()
            try:
                j = json.loads(stripped)
            except json.JSONDecodeError as e:
                last_err = f"输出不是合法 JSON（{e}；前 80 字符：{content[:80]!r}）"
                j = None

        if isinstance(j, dict) and "status_summary" in j and "breakthrough_strategy" in j:
            return j
        if j is not None:
            last_err = f"输出缺少必需字段（得到键：{sorted(j.keys()) if isinstance(j, dict) else type(j).__name__}）"
        print(f"    ⚠ 第 {attempt + 1} 次输出不合格：{last_err}")
        if attempt < RETRY_ON_BAD_JSON:
            cur_prompt = (f"你上一次的输出不合格：{last_err}\n"
                          f"请重新输出，JSON 必须且只能包含 status_summary 和 "
                          f"breakthrough_strategy 两个顶层键。\n\n" + prompt)
    raise RuntimeError(f"模型连续 {1 + RETRY_ON_BAD_JSON} 次输出不合格：{last_err}")


# ================================================================
# ② 高价值源文件选取与截取
# ================================================================

def list_project_files(proj: Path) -> list[Path]:
    out = []
    for f in proj.rglob("*"):
        if not f.is_file():
            continue
        rel = f.relative_to(proj)
        if any(part in SKIP_DIRS for part in rel.parts):
            continue
        if (f.suffix.lower() in CODE_EXTS
                or f.name in CORE_FILE_NAMES
                or f.suffix.lower() == ".md"):
            out.append(f)
    return out


def pick_core_files(proj: Path) -> list[Path]:
    """优先级：固定名单（含 HAL 配置头）> 关键词模块 > 最大的代码文件"""
    files = list_project_files(proj)
    if not files:
        return []

    chosen: list[Path] = []
    for name in CORE_FILE_NAMES:
        hits = [f for f in files if f.name == name]
        if hits:
            chosen.append(min(hits, key=lambda p: len(p.parts)))  # 路径最浅的
        if len(chosen) >= MAX_FILES:
            return chosen

    def line_count(p: Path) -> int:
        try:
            return sum(1 for _ in p.open("rb"))
        except OSError:
            return 0

    kw = [f for f in files
          if f not in chosen and f.suffix.lower() in CODE_EXTS
          and any(k in f.stem.lower() for k in KEYWORD_HINTS)]
    for f in sorted(kw, key=line_count, reverse=True):
        if len(chosen) >= MAX_FILES:
            break
        chosen.append(f)

    if len(chosen) < MAX_FILES:
        rest = [f for f in files if f not in chosen and f.suffix.lower() in CODE_EXTS]
        for f in sorted(rest, key=line_count, reverse=True):
            if len(chosen) >= MAX_FILES:
                break
            chosen.append(f)
    return chosen


def read_head(f: Path) -> str:
    """读前 MAX_LINES 行（硬上限 MAX_CHARS_PER_FILE 字符），带截断标注"""
    try:
        lines = f.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    body = "\n".join(lines[:MAX_LINES])[:MAX_CHARS_PER_FILE]
    note = f"\n…（共 {len(lines)} 行，已截取前 {MAX_LINES} 行）" if len(lines) > MAX_LINES else ""
    return body + note


def build_prompt(proj: Path, files: list[Path]) -> str:
    parts = [f"项目目录：{proj.name}", "", "=" * 40, ""]
    budget = TOTAL_BUDGET
    for f in files:
        content = read_head(f)
        if not content:
            continue
        if len(content) > budget:
            content = content[:budget] + "\n…（预算截断）"
            budget = 0
        else:
            budget -= len(content)
        parts.append(f"─── 文件：{f.relative_to(proj)} ───\n```\n{content}\n```\n")
        if budget <= 0:
            parts.append("（已达上下文预算，其余文件省略）")
            break
    parts.append(OUTPUT_SCHEMA_INSTRUCTION)   # schema 放末尾：对抗内嵌 JSON 污染
    return "\n".join(parts)


# ================================================================
# ③ 回写（正则逐段替换，单段失败不影响其他段）
# ================================================================

def esc(s: str) -> str:
    """表格/列表单元格转义：竖线和换行破坏 Markdown 结构"""
    return str(s).replace("|", "\\|").replace("\n", " ")


def apply_fill(doc: str, j: dict) -> tuple[str, list[str]]:
    applied = []
    summary = esc(j.get("status_summary", "")).strip()
    strategies = [esc(s) for s in j.get("breakthrough_strategy", []) if str(s).strip()]

    new_doc = doc
    if RE_STATUS_SUMMARY.search(new_doc):
        new_doc = RE_STATUS_SUMMARY.sub(
            f"- **整体状态**：半成品（AI 自动评估）", new_doc, count=1)
        applied.append("§2.1 整体状态")
    if RE_USABILITY.search(new_doc):
        new_doc = RE_USABILITY.sub(
            f"- **可用性**：{summary}", new_doc, count=1)
        applied.append("§2.1 可用性")
    if RE_FOUR_Q.search(new_doc):
        q_lines = []
        if strategies:
            q_lines.append(f"1. **卡点还在吗**：AI 判断——{summary}")
            q_lines.append(f"2. **最小可行动作**：{strategies[0] if len(strategies) > 0 else ''}")
            if len(strategies) > 1:
                q_lines.append(f"3. **要不要降级**：{strategies[1]}")
            if len(strategies) > 2:
                q_lines.append(f"4. **还是该归档**：{strategies[2]}")
        while len(q_lines) < 4:
            q_lines.append(f"{len(q_lines) + 1}. **（待人工补充**，AI 未给出该条）")
        new_doc = RE_FOUR_Q.sub("\n".join(q_lines), new_doc, count=1)
        applied.append("§4.2 破局四问")
    return new_doc, applied


# ================================================================
# ④ 单文档处理流水线
# ================================================================

def process_doc(md_path: Path, dry_run: bool, rebuild: bool,
                backup_dir: Path) -> bool:
    doc = md_path.read_text(encoding="utf-8")

    if not rebuild and not any(m in doc for m in PLACEHOLDER_MARKS):
        print(f"  ↷ 跳过 {md_path.name}：占位符已填写（人工或历史填写）")
        return False

    m = RE_META_PATH.search(doc)
    if not m:
        print(f"  ✗ 跳过 {md_path.name}：元数据中未找到「本地路径」行")
        return False
    proj = Path(m.group(1))
    if not proj.is_dir():
        print(f"  ✗ 跳过 {md_path.name}：项目目录不存在 → {proj}")
        return False

    files = pick_core_files(proj)
    if not files:
        print(f"  ✗ 跳过 {md_path.name}：{proj} 下没有可读的源码文件")
        return False
    print(f"  ◈ {md_path.name} ← {proj.name}，抓取 {len(files)} 个文件："
          f"{', '.join(f.relative_to(proj).as_posix() for f in files)}")

    if dry_run:
        return False

    prompt = build_prompt(proj, files)
    print(f"    调用 {CHAT_MODEL}（输入约 {len(prompt)} 字符）…", flush=True)
    try:
        j = ollama_generate_json(SYSTEM_PROMPT, prompt)
    except RuntimeError as e:
        print(f"    ✗ 跳过（模型侧错误，原文件未动）：{e}")
        return False

    new_doc, applied = apply_fill(doc, j)
    if not applied:
        print(f"    ✗ 模型已返回，但没有占位符被匹配（文档结构与模板不符？）")
        return False

    # 先备份再写回（时间戳后缀，永不覆盖）
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    shutil.copy2(md_path, backup_dir / f"{md_path.stem}.{stamp}.md")
    md_path.write_text(new_doc, encoding="utf-8")
    print(f"    ✅ 已回写：{', '.join(applied)}（备份 → {backup_dir.name}/）")
    return True


# ================================================================
# ⑤ CLI
# ================================================================

def main() -> int:
    import os
    global OLLAMA_URL
    env_host = os.environ.get("OLLAMA_HOST", "").strip()
    if env_host:  # 兼容 0.0.0.0 / 192.168.1.5:11434 等写法
        if not env_host.startswith(("http://", "https://")):
            env_host = "http://" + env_host
        env_host = env_host.replace("0.0.0.0", "127.0.0.1").rstrip("/")
        OLLAMA_URL = env_host + "/api/generate"

    ap = argparse.ArgumentParser(description="AI 自动填写复盘文档 §2/§4.2（v2, /api/generate）")
    ap.add_argument("--docs-dir", default="./kb-docs", help="复盘文档目录（默认 ./kb-docs）")
    ap.add_argument("--only", help="只处理指定项目名的文档（如 monitor-lab）")
    ap.add_argument("--dry-run", action="store_true", help="只显示会抓哪些文件，不调模型不写回")
    ap.add_argument("--rebuild", action="store_true", help="忽略已填写检查，强制重填（仍会先备份）")
    ap.add_argument("--backup-dir", default="./kb-docs-backup", help="写回前备份目录")
    args = ap.parse_args()

    docs_dir = Path(args.docs_dir).expanduser().resolve()
    if not docs_dir.is_dir():
        print(f"[错误] 文档目录不存在：{docs_dir}")
        return 1

    md_files = sorted(docs_dir.glob("*.md"))
    if args.only:
        md_files = [p for p in md_files if p.stem == args.only] or \
                   [p for p in md_files if args.only.lower() in p.stem.lower()]
    md_files = [p for p in md_files if p.name != "INDEX.md"]  # 索引页没有占位符结构
    if not md_files:
        print("[错误] 没有匹配的 .md 文档")
        return 1

    mode = "DRY-RUN" if args.dry_run else ("REBUILD" if args.rebuild else "FILL")
    print(f"== auto_fill_docs v2 [{mode}] == 目标 {len(md_files)} 份，"
          f"模型 {CHAT_MODEL} @ {OLLAMA_URL}\n")
    t0 = time.time()
    ok = 0
    for md_path in md_files:
        try:
            if process_doc(md_path, args.dry_run, args.rebuild,
                           Path(args.backup_dir).resolve()):
                ok += 1
        except Exception as e:  # 任何未预期异常：报告并保住原文件
            print(f"    ✗ 未预期异常（原文件未动）：{type(e).__name__}: {e}")
    print(f"\n完成：{ok}/{len(md_files)} 份填写，耗时 {time.time() - t0:.0f}s。")
    print("人工复核建议：AI 评估基于源码推断，状态判断请以「AI 自动评估」标注为准核对。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
