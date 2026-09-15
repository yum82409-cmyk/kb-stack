# MCP 服务接入指南（kb-rag · 项目代码助教）v2

把本地 RAG 知识库挂进 VS Code / Cursor 的 AI 对话框，两个工具：

| 工具 | 耗时 | 用途 |
|---|---|---|
| `search_knowledge_base(query, top_k)` | <1 秒 | 纯向量检索，返回文档片段+相似度 |
| `ask_knowledge_base(query, top_k)` | 10-60 秒 | 检索 + 本地 qwen3 总结（附参考来源） |

## 依赖安装

```bash
pip install mcp chromadb requests
```

（`mcp` 是官方 Python SDK；`chromadb`/`requests` 是 kb_rag 的依赖。当前机器均已就绪：mcp 1.28.1）

## v2 的 stdout 纯净度防护（为什么值得升级）

MCP stdio 传输中 stdout 是 JSON-RPC 独占管道，任何一行非 JSON 输出都会让编辑器解析崩溃。v2 在服务启动时安装了「双面 stdout 替身」：

- **`print` / 库的 Python 输出** → 自动改道 stderr（哪怕出现在工具执行期、`redirect_stdout` 之外）
- **C 扩展直接写 fd 1**（onnxruntime/sqlite3 底层告警）→ OS 层 `dup2` 物理导流到 stderr
- **SDK 的协议输出**（走 `sys.stdout.buffer`）→ 独立 fd，不受影响

已做注入式压力测试：三种典型脏输出同时触发，stdout 每一行仍可 `json.loads` 解析。这一层是自动生效的，你不需要做任何配置。

## 一、前置条件（一次性）

1. Ollama 正在运行，且两个模型已拉取：`qwen3:8b`、`bge-m3`
2. 知识库索引已建立：
   ```
   cd /path/to/kb-stack
   py kb_rag.py index kb-docs
   ```
3. MCP SDK 已安装（当前机器已完成）：`pip install mcp`

## 二、配置安装

配置内容见同目录 `mcp-config-template.json`，核心就是这段：

```json
{
  "mcpServers": {
    "kb-rag": {
      "command": "<ABSOLUTE_PATH_TO_YOUR_PYTHON>",
      "args": ["<ABSOLUTE_PATH_TO_REPO>/mcp_server.py"],
      "env": { "OLLAMA_HOST": "127.0.0.1:11434", "PYTHONIOENCODING": "utf-8" }
    }
  }
}
```

**安装位置（三选一或都装）：**

| 编辑器 | 位置 |
|---|---|
| VS Code + Cline | 命令面板 → `Cline: Open MCP Settings`，粘贴上面的 `mcpServers` 段 |
| VS Code 原生（1.99+） | `settings.json` 里用 `mcp.servers` 键，格式同上 |
| Cursor | Settings → MCP → Add Server；或编辑 `~/.cursor/mcp.json` |

## 三、对话框触发测试

1. 重启编辑器（或点 MCP 面板的刷新图标），等待 `kb-rag` 变绿/显示已连接
2. 确认工具列表里出现 `search_knowledge_base` 和 `ask_knowledge_base`
3. 在 AI 对话框依次输入：

   **第一次（快）** —— 触发纯检索：
   > 在我的知识库里搜一下「OCR 识别」相关的项目记录

   **第二次（慢，等 30-60 秒）** —— 触发问答总结：
   > 用知识库问答工具总结 monitor-lab 这个半成品项目的开发进度和缺失模块

   **预期**：AI 调用工具 → 返回带「参考来源」和「【开发进度】【核心接口】」结构的回答。

4. 如果 AI 不主动调工具，显式点名：
   > 调用 search_knowledge_base 工具，查询「登录模块」

## 四、排障速查

| 症状 | 原因与解法 |
|---|---|
| 服务连不上/立即退出 | `command` 路径错（含空格的 Python 路径必须双反斜杠转义）；在终端手动跑 `py mcp_server.py` 看报错 |
| 工具调用一直转圈 | Ollama 没起（检查 `http://127.0.0.1:11434`）或系统 `OLLAMA_HOST=0.0.0.0` 干扰（配置里的 env 已覆盖） |
| 检索结果陈旧 | 文档更新后重建索引：`py kb_rag.py index kb-docs` |
| 中文乱码 | 确认 env 里 `PYTHONIOENCODING=utf-8`（模板已含） |
| 首次连接慢 2-5 秒 | 正常：ChromaDB 预热（这是防死锁的必要设计，见 mcp_server.py 头注释） |

## 五、知识更新流程

```
改了 kb-docs 里的文档（或用 auto_fill_docs.py 重新生成）
        ↓
py kb_rag.py index kb-docs        ← 增量更新，秒级
        ↓
编辑器里的 MCP 工具立即读到新内容（每次调用实时查库，无缓存）
```
