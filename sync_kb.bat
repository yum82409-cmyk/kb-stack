@echo off
setlocal EnableExtensions EnableDelayedExpansion

rem ============================================================
rem kb-stack Windows 同步流水线
rem 顺序：项目扫描 -> AI 自动填写 -> ChromaDB 增量索引
rem 任一步返回非 0 都会立即停止，不执行后续步骤。
rem ============================================================

chcp 65001 >nul

set "SCRIPT_DIR=%~dp0"
set "PYTHON_EXE=python"   rem 或写你的解释器绝对路径
set "WORKSPACE_ROOT=%SCRIPT_DIR%.."
set "KB_DOCS=%SCRIPT_DIR%kb-docs"
set "BACKUP_DIR=%SCRIPT_DIR%kb-docs-backup"
set "COLLECTION=kb-projects"
set "AUTHOR=Your Name"

pushd "%SCRIPT_DIR%" >nul 2>&1
if errorlevel 1 goto :fail_workdir

if not exist "%PYTHON_EXE%" (
    echo [失败] 找不到 Python 解释器：%PYTHON_EXE%
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

if not exist "%WORKSPACE_ROOT%" (
    echo [失败] 找不到待扫描的工作区：%WORKSPACE_ROOT%
    goto :fail
)

set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"

echo.
echo ============================================================
echo [1/3] 扫描项目并生成 kb-docs 文档骨架
 echo      根目录：%WORKSPACE_ROOT%
echo ============================================================
call "%PYTHON_EXE%" "%SCRIPT_DIR%project_intake.py" "%WORKSPACE_ROOT%" -o "%KB_DOCS%" --author "%AUTHOR%"
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
