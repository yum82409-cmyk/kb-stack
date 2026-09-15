#!/usr/bin/env bash
# ============================================================
# 个人知识库系统 — 一键初始化脚本
# 作用：创建目录树 / 修正属主权限 / 拉模型 / 启动服务 / 健康检查
# 用法：cd kb-stack && sudo bash init.sh
# 前置：已安装 Docker 与 Compose v2；.env 已从 .env.example 复制并修改
# ============================================================
set -euo pipefail

# ---------- 0. 加载 .env ----------
if [[ ! -f .env ]]; then
  echo "❌ 未找到 .env —— 请先: cp .env.example .env 并填写"
  exit 1
fi
set -a; source .env; set +a

echo "==> 使用 DATA_ROOT=${DATA_ROOT}  REPOS_DIR=${REPOS_DIR}  PUID=${PUID}"

# ---------- 1. 创建目录树（数据全部落盘在宿主机） ----------
# SiYuan 工作区：必须是空目录或已有 .sy 数据；属主必须是 PUID:PGID
mkdir -p "${DATA_ROOT}/siyuan/workspace"

# AnythingLLM：storage（向量库+配置库）/ hotdir（解析暂存）/ outputs
mkdir -p "${DATA_ROOT}/anythingllm/storage" \
         "${DATA_ROOT}/anythingllm/hotdir" \
         "${DATA_ROOT}/anythingllm/outputs"

# Ollama 模型权重目录 + Caddy 证书目录
mkdir -p "${DATA_ROOT}/ollama" "${DATA_ROOT}/caddy/data" "${DATA_ROOT}/caddy/config"

# ---------- 2. 权限修正（最容易翻车的一步） ----------
# 原则：容器内进程以 PUID:PGID 运行，宿主机目录属主必须与之一致。
# .env 里 PUID/PGID 填你自己的 `id -u` / `id -g`，就天然无权限问题。
chown -R "${PUID}:${PGID}" "${DATA_ROOT}/siyuan" \
                              "${DATA_ROOT}/anythingllm" \
                              "${DATA_ROOT}/ollama" \
                              "${DATA_ROOT}/caddy"

# 检查零散项目目录是否存在且可读（不 chown、不写入——源码目录保持只读挂载）
if [[ ! -d "${REPOS_DIR}" ]]; then
  echo "❌ REPOS_DIR=${REPOS_DIR} 不存在，请检查 .env"
  exit 1
fi
chmod -R a+rX "${REPOS_DIR}"   # 递归加「所有人可读+目录可进入」，不改动写权限
echo "✅ 目录与权限就绪"

# ---------- 3. 启动容器栈 ----------
docker compose pull
docker compose up -d
echo "⏳ 等待服务就绪（首次启动 Ollama/AnythingLLM 需 1-2 分钟）..."
sleep 30

# ---------- 4. 拉取模型（经容器网络内的 Ollama） ----------
# 对话模型 + 中文优化的嵌入模型；显存不够就换小 tag（qwen3:8b 等）
echo "==> 拉取模型（大模型数 GB，耐心等待；失败可重跑本脚本，已下载不会重复）"
docker exec ollama ollama pull "${OLLAMA_MODEL_PREF}"
docker exec ollama ollama pull "${EMBEDDING_MODEL_PREF}"

# ---------- 5. 健康检查 ----------
echo "==> 健康检查"
docker compose ps
curl -sf "http://127.0.0.1:6806" >/dev/null && echo "✅ SiYuan    http://127.0.0.1:6806  (密码见 .env: SIYUAN_ACCESS_AUTH_CODE)"
curl -sf "http://127.0.0.1:3001/api/ping" >/dev/null && echo "✅ AnythingLLM http://127.0.0.1:3001  (首次进入创建管理员账号)"
docker exec ollama ollama list

cat <<'EOF'

🎉 初始化完成。后续动作：
  1. 浏览器打开 SiYuan    → 输入锁屏密码 → 建笔记本开始写复盘
  2. 浏览器打开 AnythingLLM → 创建管理员 → 左下角确认 LLM/Embedder 均为 Ollama 且模型名正确
  3. AnythingLLM 建工作区（如 per-project），上传/同步源码文档
  4. 公网访问：把 kb.example.com/rag.example.com DNS 指向本机，Caddy 自动签证书
     （无公网 IP → 改用 Cloudflare Tunnel，见部署文档第 3 节）
EOF
