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
    py project_intake.py <工程根目录> [<更多根目录>...] [-o 输出目录] [--author 名字]
    py project_intake.py ./my-projects -o ./kb-docs --author "Your Name"

    # 多目录（原生支持，无需符号链接）：
    py project_intake.py "F:\\Zcode Workplace" "D:\\STM32小车二次开发" -o ./kb-docs

    # 不传路径时自动读 .env 的 REPOS_DIRS（逗号分隔），再退回脚本同级目录
    py project_intake.py -o ./kb-docs

无第三方依赖，仅用标准库。Windows/Linux 通用。
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
from pathlib import Path

# ---------------------------------------------------------------
# 配置区
# ---------------------------------------------------------------

# 默认扫描深度：1 = 只看根目录的一级子目录（保持原有行为）
#                 2 = 再下探一层（适配 D:\工作区\01-工程\<各Keil工程> 这类两层结构）
DEFAULT_DEPTH = 1

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


# ---------------------------------------------------------------
# 多根目录配置解析（.env / 常量 / 命令行，三级优先级）
# ---------------------------------------------------------------

def parse_dotenv(env_path: Path) -> dict[str, str]:
    """极简 .env 解析：KEY=VALUE，忽略注释与空行，去除成对引号。
    不引入 python-dotenv 依赖（保持零第三方依赖）。"""
    result: dict[str, str] = {}
    if not env_path.is_file():
        return result
    try:
        for raw in env_path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key, val = key.strip(), val.strip()
            # 去掉行尾注释（值里含 # 的路径罕见，仅在 # 前有空格时才截断）
            if " #" in val:
                val = val.split(" #", 1)[0].strip()
            if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
                val = val[1:-1]
            if key:
                result[key] = val
    except OSError:
        pass
    return result


def split_paths(raw: str) -> list[str]:
    """按逗号/分号切分路径列表（兼容中英文标点），逐个 strip。
    Windows 盘符里的冒号不受影响（只切逗号与分号）。"""
    if not raw:
        return []
    parts = re.split(r"[,;，；]", raw)
    return [p.strip().strip("\"'") for p in parts if p.strip()]


def resolve_roots(cli_roots: list[str], script_dir: Path) -> tuple[list[Path], str]:
    """确定要扫描的根目录列表，返回 (roots, 来源说明)。

    优先级：
      1. 命令行传入的路径（可多个）
      2. .env 的 REPOS_DIRS（逗号分隔）
      3. 脚本同级目录（保持单人单目录场景开箱即用）
    """
    if cli_roots:
        candidates, source = cli_roots, "命令行参数"
    else:
        env_path = script_dir / ".env"
        env = parse_dotenv(env_path)
        raw = env.get("REPOS_DIRS") or env.get("REPOS_DIR") or ""
        candidates = split_paths(raw)
        if candidates:
            source = f".env ({env_path.name} 的 REPOS_DIRS)"
        else:
            candidates, source = [str(script_dir)], "脚本同级目录（未配置 REPOS_DIRS）"

    roots: list[Path] = []
    for c in candidates:
        p = Path(c).expanduser()
        try:
            p = p.resolve()
        except OSError:
            pass
        if p.is_dir():
            if p not in roots:          # 去重（同一路径写两次只扫一次）
                roots.append(p)
        else:
            print(f"  [跳过] 目录不存在：{p}")
    return roots, source


def has_direct_markers(d: Path) -> bool:
    """目录内【直接】含项目标志：依赖文件 / README / .git / 直接躺着的源码。
    这是「这是一个工程」的强信号，不靠下探猜测。"""
    try:
        entries = list(d.iterdir())
    except (PermissionError, OSError):
        return False
    names = {e.name for e in entries}
    if any(n in names for n in DEPENDENCY_FILES) or "README.md" in names or ".git" in names:
        return True
    return any(e.is_file() and e.suffix.lower() in CODE_EXTS for e in entries)


def has_code_below(d: Path) -> bool:
    """目录的【下一层】是否有源码（弱信号：说明它可能是工程，也可能是分组目录）"""
    try:
        for e in d.iterdir():
            if e.is_dir() and not is_skip_dir(e.name) and not e.name.startswith("."):
                if any(f.is_file() and f.suffix.lower() in CODE_EXTS
                       for f in e.iterdir()):
                    return True
    except (PermissionError, OSError):
        pass
    return False


def find_projects(root: Path, depth: int = DEFAULT_DEPTH) -> list[Path]:
    """在 root 下查找项目目录（root 自身不算项目，只扫其子目录）。

    判定规则（解决"工作区根目录被误判为项目"的问题）：
      · 子目录含【直接标志】（依赖文件/README/.git/直接源码）→ 判定为项目；
      · 否则若还有深度余量 → 视为分组目录继续下探
        （适配 D:\\工作区\\01-工程\\<各Keil工程> 两层结构）；
      · 深度用尽但下一层有源码 → 仍收录，避免漏掉浅层工程。

    depth=1：只看一级子目录（与原行为一致）
    depth=2：再下探一层分组目录
    """
    projects: list[Path] = []

    def scan(parent: Path, level: int) -> None:
        try:
            children = sorted(parent.iterdir())
        except (PermissionError, OSError):
            return
        for child in children:
            if not child.is_dir() or is_skip_dir(child.name) or child.name.startswith("."):
                continue
            if has_direct_markers(child):
                projects.append(child)              # 强信号：就是工程
            elif level < depth:
                scan(child, level + 1)              # 分组目录：继续下探
            elif has_code_below(child):
                projects.append(child)              # 深度用尽：收录浅层工程

    scan(root, 1)
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


def generate_doc(proj: Path, author: str, source_root: Path | None = None,
                 out_path: Path | None = None) -> tuple[str, dict]:
    """为单个项目生成复盘文档；返回 (markdown, 元信息dict用于INDEX)

    source_root：该项目来自哪个扫描根目录（多根模式下写入附录，便于溯源）。
    out_path   ：目标文件路径。若已存在且含已填写内容，则【保留人工/AI 填写区】，
                 仅刷新第 5 节附录 —— 避免重跑流水线时冲掉既有成果。
    """
    meta = {
        "name": proj.name,
        "path": str(proj),
        "last_active": file_mtime(proj),
        "tech": detect_tech_hint(proj),
        "source_root": str(source_root) if source_root else str(proj.parent),
    }

    auto_lines = [
        f"项目目录：{proj}",
        f"来源扫描根：{meta['source_root']}",
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

    # ---- 增量保护：已有文档且含填写内容 → 只换附录，保留 §1-§4 ----
    if out_path and out_path.is_file():
        try:
            existing = out_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            existing = ""
        if existing and has_user_content(existing):
            refreshed = replace_appendix(existing, auto_section)
            if refreshed is not None:
                meta["preserved"] = True
                return refreshed, meta

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


# 附录起点标志（模板第 5 节标题）
APPENDIX_MARKER = "## 5. 附录：自动抓取的原始信息"


def has_user_content(doc: str) -> bool:
    """判断文档是否已有【人工/AI 已填写】的核心内容。

    判据只看 AI/人工真正会填的两处，忽略 §1 元数据区那些"本就留白待填"的占位符
    （如 {{YYYY-MM}}、{{这个项目是干什么的}} —— 它们长期存在属正常）：
      · §2.1 整体状态：是否还是模板默认的 {{半成品-搁置}}
      · §4.2 破局四问：是否还是模板默认的 {{当时的障碍是客观的...}}
    任一已被替换 → 视为已填写，重跑时保留主体、只刷新附录。
    """
    return ("{{半成品-搁置}}" not in doc) or ("{{当时的障碍是客观的" not in doc)


def replace_appendix(doc: str, auto_section: str) -> str | None:
    """只替换第 5 节附录的 text 代码块内容，保留其余章节。
    找不到附录结构时返回 None（调用方回退到全量重建）。"""
    idx = doc.find(APPENDIX_MARKER)
    if idx < 0:
        return None
    head = doc[:idx]
    tail = doc[idx:]
    # 替换附录内 ```text ... ``` 的内容（取第一个代码块）
    m = re.search(r"```(?:text)?\n(.*?)\n```", tail, re.S)
    if not m:
        return None
    new_tail = tail[:m.start(1)] + auto_section + tail[m.end(1):]
    return head + new_tail


def generate_index(docs: list[tuple[str, dict]], out_dir: Path,
                   roots: list[Path] | None = None) -> str:
    """汇总索引：总览表 + 状态填写区 + 扫描来源说明"""
    today = dt.date.today().isoformat()
    lines = [
        "# 半成品项目知识库 · 总索引",
        "",
        f"> 生成日期：{today}　项目数：{len(docs)}　",
        "> 由 `project_intake.py` 自动生成；「状态/一句话定位」需人工填写。",
        "",
    ]
    # 多根模式：列出所有扫描来源，便于溯源
    if roots:
        lines += ["**扫描来源**：", ""]
        lines += [f"- `{r}`" for r in roots]
        lines.append("")

    lines += [
        "## 项目总览",
        "",
        "| # | 项目 | 状态 | 一句话定位 | 技术栈 | 最后活跃 | 来源 | 文档 |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for i, (_, m) in enumerate(docs, 1):
        src = Path(m.get("source_root", "")).name or "-"
        lines.append(
            f"| {i} | {m['name']} | ☐待填 | 待填 | {m['tech']} | "
            f"{m['last_active']} | {src} | [{m['name']}.md]({m['name']}.md) |"
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
    ap = argparse.ArgumentParser(
        description="半成品项目知识库录入生成器（支持多根目录）",
        epilog="不传 root 时自动读取 .env 的 REPOS_DIRS（逗号分隔），再退回脚本同级目录。")
    ap.add_argument("root", nargs="*",
                    help="工程根目录，可传多个；也可用 .env 的 REPOS_DIRS 配置")
    ap.add_argument("-o", "--out", default="./kb-docs", help="输出目录（默认 ./kb-docs）")
    ap.add_argument("--author", default="我", help="录入人名字（默认「我」）")
    ap.add_argument("--depth", type=int, default=DEFAULT_DEPTH,
                    help=f"扫描深度：1=仅一级子目录（默认），2=再下探一层分组目录")
    ap.add_argument("--open", action="store_true", help="生成后用资源管理器打开输出目录")
    args = ap.parse_args()

    script_dir = Path(__file__).resolve().parent
    if not TEMPLATE_PATH.exists():
        print(f"[错误] 未找到模板：{TEMPLATE_PATH}（应与脚本同目录的 templates/ 下）")
        return 2

    # ---- 解析扫描根目录（命令行 > .env REPOS_DIRS > 脚本同级目录）----
    roots, source = resolve_roots(args.root, script_dir)
    if not roots:
        print("[错误] 没有可扫描的目录。请传路径参数，或在 .env 配置 REPOS_DIRS。")
        return 1
    print(f"扫描来源（{source}）：")
    for r in roots:
        print(f"  · {r}")
    print()

    out_dir = Path(args.out).expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    # ---- 外层循环：逐个根目录扫描，统一收集（含重名去重）----
    docs_meta: list[tuple[str, dict]] = []
    used_names: dict[str, int] = {}      # 文件名 → 已用次数（重名时加来源后缀）
    total_found = 0

    for root in roots:
        projects = find_projects(root, depth=args.depth)
        if not projects:
            print(f"[提示] {root} 下没有识别出项目目录，跳过。")
            continue
        print(f"== {root} → 识别出 {len(projects)} 个项目 ==")
        for proj in projects:
            total_found += 1

            # 重名处理：不同根目录下同名项目 → 文件名加来源目录名后缀
            stem = proj.name
            if stem in used_names:
                used_names[stem] += 1
                stem = f"{proj.name}（{root.name}）"
            else:
                used_names[stem] = 1

            out_path = out_dir / f"{stem}.md"
            doc, meta = generate_doc(proj, args.author, source_root=root,
                                     out_path=out_path)
            if stem != proj.name:
                meta["name"] = stem
                doc = doc.replace(f"# {proj.name} ——", f"# {stem} ——", 1)

            out_path.write_text(doc, encoding="utf-8")
            docs_meta.append((f"{stem}.md", meta))
            tag = "（已保留填写内容，仅刷新附录）" if meta.get("preserved") else ""
            print(f"  → {stem} ... OK{tag}")

    if not docs_meta:
        print(f"\n[提示] 所有根目录下均未识别出项目（无代码/README/依赖文件特征）。")
        return 0

    index_md = generate_index(docs_meta, out_dir, roots=roots)
    (out_dir / "INDEX.md").write_text(index_md, encoding="utf-8")
    print(f"\n完成：{len(docs_meta)} 份复盘文档 + INDEX.md → {out_dir}")
    if total_found != len(docs_meta):
        print(f"（重名合并：{total_found} 个项目中 {total_found - len(docs_meta)} 个因同名已加后缀区分）")

    if args.open:
        import webbrowser
        webbrowser.open(out_dir.as_uri())
    return 0


if __name__ == "__main__":
    sys.exit(main())
