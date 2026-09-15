#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
project_intake.py —— 半成品项目知识库录入生成器

遍历本地工程目录，为每个「项目文件夹」生成一份《项目复盘与交接文档》：
  - 抓取 README.md 首部作为项目简介
  - 解析依赖文件（requirements.txt / CMakeLists.txt / platformio.ini /
    pyproject.toml / *.ioc / Makefile / go.mod / package.json）
  - 统计代码规模（语言 → 文件数/行数，排除 venv/node_modules 等）
  - 提取 Git 元数据（首次/最后提交时间、提交数，有 git 命令时）
  - 检测嵌入式特征（*.ioc → STM32CubeMX，platformio.ini → PlatformIO）
  - 生成 <项目名>.md（模板四层级 + 附录自动信息）
  - 汇总 INDEX.md 索引（表格 + 状态统计），可直接导入 SiYuan / AnythingLLM

用法：
    py project_intake.py <工程根目录> [-o 输出目录] [--author 名字] [--open]
    py project_intake.py ./my-projects -o ./kb-docs --author "Your Name"

无第三方依赖，仅用标准库。Windows/Linux 通用。
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import subprocess
import sys
from pathlib import Path

# ---------------------------------------------------------------
# 配置区
# ---------------------------------------------------------------

# 扫描时跳过的目录名（工具缓存、虚拟环境、IDE 垃圾）
SKIP_DIRS = {
    "node_modules", "venv", ".venv", "env", "__pycache__", ".git",
    ".idea", ".vscode", ".vs", "build", "dist", "target", "cmake-build-debug",
    "Dependencies", ".cache", "logs", "*.egg-info",
}

# 统计代码规模时计入的扩展名 → 语言名
CODE_EXTS = {
    ".py": "Python", ".c": "C", ".h": "C", ".cpp": "C++", ".cc": "C++",
    ".hpp": "C++", ".ino": "Arduino/C++", ".s": "ASM", ".ld": "LinkerScript",
    ".sh": "Shell", ".bat": "Batch", ".ps1": "PowerShell",
    ".js": "JavaScript", ".ts": "TypeScript", ".go": "Go", ".rs": "Rust",
    ".java": "Java", ".lua": "Lua",
}

# 依赖/构建文件 → 抓取函数名（下方 DEPENDENCY_PARSERS 分发）
DEPENDENCY_FILES = [
    "requirements.txt", "pyproject.toml", "CMakeLists.txt", "Makefile",
    "platformio.ini", "go.mod", "package.json",
]

TEMPLATE_PATH = Path(__file__).parent / "templates" / "PROJECT_TEMPLATE.md"

# ---------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------

def is_skip_dir(name: str) -> bool:
    """目录名命中 SKIP_DIRS（支持 fnmatch 风格 *.xxx）则跳过"""
    from fnmatch import fnmatch
    return any(fnmatch(name, pat) for pat in SKIP_DIRS)


def find_projects(root: Path) -> list[Path]:
    """一级子目录中，含代码/文档特征的视为项目；返回[根目录]代表根本身也是项目"""
    projects = []
    for child in sorted(root.iterdir()):
        if not child.is_dir() or is_skip_dir(child.name) or child.name.startswith("."):
            continue
        if looks_like_project(child):
            projects.append(child)
    return projects


def looks_like_project(d: Path) -> bool:
    """判定目录是否像一个工程：含代码文件/依赖文件/README/git 任一即算"""
    try:
        entries = list(d.iterdir())
    except PermissionError:
        return False
    names = {e.name for e in entries}
    if any(n in names for n in DEPENDENCY_FILES) or "README.md" in names or ".git" in names:
        return True
    if any(e.suffix.lower() in CODE_EXTS for e in entries if e.is_file()):
        return True
    # 只下探一层（如 src/、monitor/ 结构）
    return any(
        e.is_dir() and not is_skip_dir(e.name) and not e.name.startswith(".")
        and any(f.suffix.lower() in CODE_EXTS for f in e.iterdir() if f.is_file())
        for e in entries
    )


def read_text_safe(p: Path, limit: int = 4000) -> str:
    """容错读文本：编码不对就降级 latin-1，绝不抛异常"""
    try:
        return p.read_text(encoding="utf-8", errors="replace")[:limit]
    except OSError:
        return ""


# ---------------------------------------------------------------
# 各类依赖文件解析器（返回 markdown 行列表）
# ---------------------------------------------------------------

def parse_requirements(p: Path) -> list[str]:
    lines = [l.strip() for l in read_text_safe(p).splitlines()
             if l.strip() and not l.startswith("#")]
    return [f"- Python 依赖（requirements.txt，共 {len(lines)} 项）："] + \
           [f"  - `{l}`" for l in lines[:15]] + (["  - …（截断）"] if len(lines) > 15 else [])


def parse_pyproject(p: Path) -> list[str]:
    text = read_text_safe(p)
    m = re.search(r'name\s*=\s*"([^"]+)"', text)
    req = re.findall(r'^\s*"([a-zA-Z0-9_.<>=!\[\]-]+)"\s*,?\s*$', text, re.M)
    out = ["- Python 项目（pyproject.toml）"]
    if m:
        out.append(f"  - 包名：`{m.group(1)}`")
    if req:
        out.append(f"  - 依赖：{', '.join(f'`{r}`' for r in req[:10])}")
    return out


def parse_cmake(p: Path) -> list[str]:
    text = read_text_safe(p, 8000)
    out = ["- C/C++ 构建（CMake）："]
    ver = re.search(r"cmake_minimum_required\(VERSION\s+([\d.]+)", text)
    proj = re.search(r"project\s*\(\s*(\w+)", text, re.I)
    mcu = re.search(r"(STM32\w+|ESP32\w*|GD32\w+|CH32\w+|nRF\w+)", text)
    if ver:
        out.append(f"  - 最低 CMake 版本：{ver.group(1)}")
    if proj:
        out.append(f"  - 工程名：{proj.group(1)}")
    if mcu:
        out.append(f"  - 目标 MCU：**{mcu.group(1)}**（从 CMake 脚本检出）")
    comps = re.findall(r"find_package\s*\(\s*(\w+)", text)
    if comps:
        out.append(f"  - find_package：{', '.join(comps[:8])}")
    return out


def parse_makefile(p: Path) -> list[str]:
    text = read_text_safe(p, 6000)
    out = ["- Makefile 构建："]
    mcu = re.search(r"(STM32\w+|ESP32\w*|GD32\w+|CH32\w+)", text)
    cross = re.search(r"(arm-none-eabi|avr-gcc|riscv\S*-gcc)", text)
    if mcu:
        out.append(f"  - 目标 MCU：**{mcu.group(1)}**")
    if cross:
        out.append(f"  - 交叉工具链：{cross.group(1)}")
    if not mcu and not cross:
        out.append("  - 常规 Makefile（未检出交叉编译特征）")
    return out


def parse_platformio(p: Path) -> list[str]:
    text = read_text_safe(p)
    out = ["- PlatformIO 嵌入式工程："]
    board = re.search(r"board\s*=\s*(\S+)", text)
    framework = re.search(r"framework\s*=\s*(\S+)", text)
    mcu = re.search(r"(STM32\w+|ESP32\w*|GD32\w+|CH32\w+|nRF\w+|ATmega\w+)", text)
    if board:
        out.append(f"  - 开发板：{board.group(1)}")
    if framework:
        out.append(f"  - 框架：{framework.group(1)}")
    if mcu:
        out.append(f"  - MCU：**{mcu.group(1)}**")
    return out


def parse_go_mod(p: Path) -> list[str]:
    text = read_text_safe(p)
    mods = re.findall(r"^\s+(\S+)\s+v\S+", text, re.M)
    return [f"- Go 依赖（go.mod，共 {len(mods)} 个模块）："] + \
           [f"  - `{m}`" for m in mods[:12]]


def parse_package_json(p: Path) -> list[str]:
    try:
        d = json.loads(read_text_safe(p))
    except json.JSONDecodeError:
        return ["- package.json（解析失败）"]
    deps = {**d.get("dependencies", {}), **d.get("devDependencies", {})}
    out = [f"- Node.js 工程：{d.get('name', '(无名)')}",
           f"  - 依赖共 {len(deps)} 项"]
    for k in list(deps)[:10]:
        out.append(f"  - `{k}@{deps[k]}`")
    return out


DEPENDENCY_PARSERS = {
    "requirements.txt": parse_requirements,
    "pyproject.toml": parse_pyproject,
    "CMakeLists.txt": parse_cmake,
    "Makefile": parse_makefile,
    "platformio.ini": parse_platformio,
    "go.mod": parse_go_mod,
    "package.json": parse_package_json,
}

# ---------------------------------------------------------------
# 信息抓取
# ---------------------------------------------------------------

def scan_dependencies(proj: Path) -> list[str]:
    """在项目内（两层深度）找依赖文件并解析"""
    found: list[str] = []
    candidates = list(proj.glob("*")) + list(proj.glob("*/*"))
    for dep_file in DEPENDENCY_FILES:
        for p in candidates:
            if p.is_file() and p.name == dep_file:
                try:
                    lines = DEPENDENCY_PARSERS[dep_file](p)
                except Exception as e:  # 单文件解析失败不阻断整体
                    lines = [f"- {dep_file}（解析异常：{e}）"]
                found.extend(lines)
                break  # 每类依赖文件只取第一个
    if not found:
        found.append("- 未发现任何依赖/构建文件（裸脚本或纯文档工程）")
    return found


def scan_code_stats(proj: Path) -> list[str]:
    """按语言统计文件数与行数（两层深度足够覆盖个人工程）"""
    stats: dict[str, list[int]] = {}
    all_files = [f for f in proj.rglob("*")
                 if f.is_file() and not any(is_skip_dir(part) for part in f.relative_to(proj).parts)]
    total_lines = 0
    for f in all_files:
        lang = CODE_EXTS.get(f.suffix.lower())
        if not lang:
            continue
        try:
            n = sum(1 for _ in f.open("rb"))
        except OSError:
            continue
        stats.setdefault(lang, [0, 0])
        stats[lang][0] += 1
        stats[lang][1] += n
        total_lines += n
    if not stats:
        return [f"- 代码规模：未发现可统计代码（{len(all_files)} 个文件）"]
    lines = [f"- 代码规模：{len(all_files)} 个文件，约 {total_lines} 行代码"]
    for lang, (nf, nl) in sorted(stats.items(), key=lambda x: -x[1][1]):
        lines.append(f"  - {lang}: {nf} 文件 / {nl} 行")
    return lines


def scan_readme(proj: Path) -> list[str]:
    """README 前若干行作为简介"""
    for name in ("README.md", "README.txt", "README"):
        p = proj / name
        if p.is_file():
            text = read_text_safe(p, 1500).strip()
            # 取到第一个二级标题前，避免整篇塞进去
            cut = text.find("\n## ")
            excerpt = text[:cut if cut > 100 else 900].strip()
            return ["- README 摘要（自动截取）：", "  ```",
                    *[f"  {l}" for l in excerpt.splitlines()[:25]], "  ```"]
    return ["- 未发现 README"]


def scan_ioc_embedded(proj: Path) -> list[str]:
    """STM32CubeMX / Keil / IAR 工程特征"""
    out = []
    iocs = list(proj.glob("**/*.ioc"))
    if iocs:
        text = read_text_safe(iocs[0], 3000)
        mcu = re.search(r"Mcu\.Name=(\S+)", text)
        out.append(f"- STM32CubeMX 工程（{len(iocs)} 个 .ioc）")
        if mcu:
            out.append(f"  - 芯片型号：**{mcu.group(1)}**")
    for pat, desc in [("*.uvprojx", "Keil MDK 工程"), ("*.ewp", "IAR EWARM 工程"),
                      ("*.code-workspace", "VS Code 工作区")]:
        hits = list(proj.glob(f"**/{pat}"))
        if hits:
            out.append(f"- {desc}（{hits[0].name}）")
    return out


def scan_git(proj: Path) -> list[str]:
    """git 元数据：首次/最后提交、提交数（无 git 或无仓库则跳过）"""
    if not (proj / ".git").exists():
        return ["- 版本控制：无 .git（未纳入 Git）"]
    def git(*args: str) -> str:
        try:
            r = subprocess.run(["git", "-C", str(proj), *args],
                              capture_output=True, text=True, timeout=10,
                              encoding="utf-8", errors="replace")
            return r.stdout.strip() if r.returncode == 0 else ""
        except Exception:
            return ""
    first = git("log", "--reverse", "--format=%ad", "--date=short")
    last = git("log", "-1", "--format=%ad", "--date=short")
    count = git("rev-list", "--count", "HEAD")
    out = ["- Git 仓库："]
    if first:
        out.append(f"  - 首次提交：{first.splitlines()[0]}")
    if last:
        out.append(f"  - 最近提交：{last}")
    if count:
        out.append(f"  - 提交总数：{count}")
    remote = git("remote", "get-url", "origin")
    if remote:
        out.append(f"  - 远程：{remote}")
    return out


def file_mtime(proj: Path) -> str:
    """目录内最新文件修改时间（排除生成物目录），作为「最后活跃」的粗估"""
    newest = ""
    for f in proj.rglob("*"):
        if f.is_file() and not any(is_skip_dir(part) for part in f.relative_to(proj).parts):
            try:
                t = dt.datetime.fromtimestamp(f.stat().st_mtime).strftime("%Y-%m-%d")
                if t > newest:
                    newest = t
            except OSError:
                pass
    return newest or "未知"


# ---------------------------------------------------------------
# 文档生成
# ---------------------------------------------------------------

def detect_tech_hint(proj: Path) -> str:
    exts = {f.suffix.lower() for f in proj.rglob("*") if f.is_file()
            and not any(is_skip_dir(p) for p in f.relative_to(proj).parts)}
    langs = sorted({CODE_EXTS[e] for e in exts if e in CODE_EXTS})
    return "、".join(langs) if langs else "未知（无代码文件）"


def generate_doc(proj: Path, author: str) -> tuple[str, dict]:
    """为单个项目生成复盘文档；返回 (markdown, 元信息dict用于INDEX)"""
    meta = {
        "name": proj.name,
        "path": str(proj),
        "last_active": file_mtime(proj),
        "tech": detect_tech_hint(proj),
    }

    auto_lines = [
        f"项目目录：{proj}",
        f"最后活跃（文件 mtime）：{meta['last_active']}",
        f"检测到的语言：{meta['tech']}",
        "",
        "【README】", *scan_readme(proj),
        "",
        "【依赖与构建】", *scan_dependencies(proj),
        *scan_ioc_embedded(proj),
        "",
        "【Git】", *scan_git(proj),
        "",
        "【代码规模】", *scan_code_stats(proj),
    ]
    auto_section = "\n".join(auto_lines)

    # 基于模板填充（模板含 {{占位符}}；附录整体替换）
    template = TEMPLATE_PATH.read_text(encoding="utf-8") if TEMPLATE_PATH.exists() else "{{AUTO_GENERATED_SECTION}}"
    today = dt.date.today().isoformat()
    doc = template.replace("{{项目名}}", proj.name)
    doc = doc.replace("{{你的名字}}", author)
    doc = doc.replace("{{YYYY-MM-DD}}", today)
    doc = doc.replace("{{绝对路径，用于回溯}}", str(proj))
    doc = doc.replace("{{AUTO_GENERATED_SECTION}}", auto_section)
    # 其余未填充占位符保留原样，方便人工补写；但顶层多级提示简化
    return doc, meta


def generate_index(docs: list[tuple[str, dict]], out_dir: Path) -> str:
    """汇总索引：总览表 + 状态填写区"""
    today = dt.date.today().isoformat()
    lines = [
        "# 半成品项目知识库 · 总索引",
        "",
        f"> 生成日期：{today}　项目数：{len(docs)}　",
        "> 由 `project_intake.py` 自动生成；「状态/一句话定位」需人工填写。",
        "",
        "## 项目总览",
        "",
        "| # | 项目 | 状态 | 一句话定位 | 技术栈 | 最后活跃 | 文档 |",
        "|---|---|---|---|---|---|---|",
    ]
    for i, (_, m) in enumerate(docs, 1):
        lines.append(
            f"| {i} | {m['name']} | ☐待填 | 待填 | {m['tech']} | "
            f"{m['last_active']} | [{m['name']}.md]({m['name']}.md) |"
        )
    lines += [
        "",
        "## 按状态归档（人工维护）",
        "",
        "```text",
        "成品：",
        "可演示：",
        "半成品-搁置：",
        "半成品-活跃：",
        "想法验证：",
        "已废弃：",
        "```",
        "",
        "---",
        "### 复盘建议顺序",
        "1. 先按「最后活跃」倒序浏览，越久未动的越需要明确「重启 or 归档」",
        "2. 每份文档至少填完 §2 状态定义和 §4.2 破局四问，才算完成录入",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description="半成品项目知识库录入生成器")
    ap.add_argument("root", help="工程根目录（其一级子目录被视为项目）")
    ap.add_argument("-o", "--out", default="./kb-docs", help="输出目录（默认 ./kb-docs）")
    ap.add_argument("--author", default="我", help="录入人名字（默认「我」）")
    ap.add_argument("--open", action="store_true", help="生成后用资源管理器打开输出目录")
    args = ap.parse_args()

    root = Path(args.root).expanduser().resolve()
    if not root.is_dir():
        print(f"[错误] 目录不存在：{root}")
        return 1
    if not TEMPLATE_PATH.exists():
        print(f"[错误] 未找到模板：{TEMPLATE_PATH}（应与脚本同目录的 templates/ 下）")
        return 2

    out_dir = Path(args.out).expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    projects = find_projects(root)
    if not projects:
        print(f"[提示] {root} 下没有识别出项目目录（无代码/README/依赖文件特征）")
        return 0

    print(f"识别出 {len(projects)} 个项目目录：")
    docs_meta: list[tuple[str, dict]] = []
    for proj in projects:
        print(f"  → {proj.name} ...", end=" ", flush=True)
        doc, meta = generate_doc(proj, args.author)
        (out_dir / f"{proj.name}.md").write_text(doc, encoding="utf-8")
        docs_meta.append((f"{proj.name}.md", meta))
        print("OK")

    index_md = generate_index(docs_meta, out_dir)
    (out_dir / "INDEX.md").write_text(index_md, encoding="utf-8")
    print(f"\n完成：{len(docs_meta)} 份复盘文档 + INDEX.md → {out_dir}")

    if args.open:
        import webbrowser
        webbrowser.open(out_dir.as_uri())
    return 0


if __name__ == "__main__":
    sys.exit(main())
