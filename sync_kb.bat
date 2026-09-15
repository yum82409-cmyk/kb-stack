@echo off
setlocal EnableExtensions EnableDelayedExpansion

rem ============================================================
rem kb-stack Windows 同步流水线（多目录版）
rem 顺序：项目扫描 -> AI 自动填写 -> ChromaDB 增量索引
rem 任一步返回非 0 都会立即停止，不执行后续步骤。
rem
rem 扫描目录来源（优先级）：
rem   1. .env 的 REPOS_DIRS（逗号分隔，可多个）
rem   2. 未配置时回退到脚本上级目录（保持旧行为）
rem 深度由 .env 的 INTAKE_DEPTH 控制（1=一级子目录，2=再下探一层）
rem ============================================================

chcp 65001 >nul

set "SCRIPT_DIR=%~dp0"
set "PYTHON_EXE=python"   rem 或写你的解释器绝对路径
set "KB_DOCS=%SCRIPT_DIR%kb-docs"
set "BACKUP_DIR=%SCRIPT_DIR%kb-docs-backup"
set "COLLECTION=kb-projects"
set "AUTHOR=Your Name"
set "REPOS_DIRS="
set "INTAKE_DEPTH=1"

pushd "%SCRIPT_DIR%" >nul 2>&1
if errorlevel 1 goto :fail_workdir

rem ---- 从 .env 读取 REPOS_DIRS / INTAKE_DEPTH / AUTHOR（存在才覆盖）----
if exist "%SCRIPT_DIR%.env" (
    for /f "usebackq tokens=1,* delims==" %%a in ("%SCRIPT_DIR%.env") do (
        set "k=%%a"
        set "v=%%b"
        if not "!k!"=="" if not "!k:~0,1!"=="#" (
            if /i "!k!"=="REPOS_DIRS"   set "REPOS_DIRS=!v!"
            if /i "!k!"=="INTAKE_DEPTH" set "INTAKE_DEPTH=!v!"
            if /i "!k!"=="AUTHOR"       set "AUTHOR=!v!"
        )
    )
)

where python >nul 2>&1
if errorlevel 1 (
    echo [失败] PATH 中找不到 python，请把本文件开头的 PYTHON_EXE 改为解释器绝对路径
    goto :fail
)

if not exist "%SCRIPT_DIR%project_intake.py" (
    echo [失败] 找不到 project_intake.py
    goto :fail
)

if not exist "%SCRIPT_DIR%auto_fill_docs.py" (
    echo [失败] 找不到 auto_fill_docs.py
    goto :fail
)

if not exist "%SCRIPT_DIR%kb_rag.py" (
    echo [失败] 找不到 kb_rag.py
    goto :fail
)

set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"

echo.
echo ============================================================
echo [1/3] 扫描项目并生成 kb-docs 文档骨架
if defined REPOS_DIRS (
    echo      扫描来源：%REPOS_DIRS%
) else (
    echo      扫描来源：脚本上级目录（.env 未配置 REPOS_DIRS）
)
echo      扫描深度：%INTAKE_DEPTH%
echo ============================================================

if defined REPOS_DIRS (
    rem 多目录：交给 project_intake.py 自行解析 .env（不传路径参数）
    call "%PYTHON_EXE%" "%SCRIPT_DIR%project_intake.py" -o "%KB_DOCS%" --author "%AUTHOR%" --depth %INTAKE_DEPTH%
) else (
    rem 兼容旧行为：显式传入上级目录，深度保持默认 1
    call "%PYTHON_EXE%" "%SCRIPT_DIR%project_intake.py" "%SCRIPT_DIR%.." -o "%KB_DOCS%" --author "%AUTHOR%"
)
if errorlevel 1 (
    echo [失败] project_intake.py 返回错误，已阻断后续步骤。
    goto :fail
)
echo [完成] 项目扫描与文档生成

echo.
echo ============================================================
echo [2/3] 调用本地模型自动填写项目状态与破局思路
echo      文档目录：%KB_DOCS%
echo ============================================================
call "%PYTHON_EXE%" "%SCRIPT_DIR%auto_fill_docs.py" --docs-dir "%KB_DOCS%" --backup-dir "%BACKUP_DIR%"
if errorlevel 1 (
    echo [失败] auto_fill_docs.py 返回错误，已阻断向量索引步骤。
    goto :fail
)
echo [完成] 自动填写流程

echo.
echo ============================================================
echo [3/3] 构建或更新 ChromaDB 向量索引
echo      集合：%COLLECTION%
echo ============================================================
call "%PYTHON_EXE%" "%SCRIPT_DIR%kb_rag.py" index "%KB_DOCS%" --collection "%COLLECTION%"
if errorlevel 1 (
    echo [失败] kb_rag.py index 返回错误。
    goto :fail
)
echo [完成] ChromaDB 向量索引已更新

echo.
echo ============================================================
echo [成功] kb-stack 同步流水线全部完成
echo      文档目录：%KB_DOCS%
echo      备份目录：%BACKUP_DIR%
echo      向量集合：%COLLECTION%
echo ============================================================
popd >nul 2>&1
exit /b 0

:fail_workdir
echo [失败] 无法进入脚本目录：%SCRIPT_DIR%
exit /b 10

:fail
popd >nul 2>&1
exit /b 1
