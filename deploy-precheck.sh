#!/usr/bin/env bash
# ============================================================
# deploy-precheck.sh —— Linux 服务器部署前置检查
# ============================================================
# 上服务器跑的第一条命令：在 docker compose up 之前把环境问题全部暴露。
# 全部检查只读不写，可反复运行。
#
# 用法：bash deploy-precheck.sh [DATA_ROOT]
#   默认 DATA_ROOT=/srv/kb（与 .env.example 一致）
#
# 检查项（12 项，致命问题 FAIL，建议项 WARN）：
#   A. 基础    docker / compose v2 / .env 是否就绪
#   B. 数据    DATA_ROOT 文件系统类型(NTNTS 致命) / 磁盘余量(模型需 ~15GB)
#   C. 网络    80/443 是否已被占用（本方案不需要它们，被占反而说明有旧服务）
#   D. 隧道    TUN 设备（Tailscale 用）/ 出站连通性（CF 与 Tailscale）
#   E. GPU     nvidia-container-toolkit（有 N 卡才查）
#   F. 工具链  python3 + curl（拉模型和健康检查用）
# ============================================================
set -uo pipefail

DATA_ROOT="${1:-/srv/kb}"
PASS=0; FAIL=0; WARN=0
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

ok()   { echo "  ✅ $1"; PASS=$((PASS+1)); }
bad()  { echo "  ❌ $1"; FAIL=$((FAIL+1)); }
warn() { echo "  ⚠️  $1"; WARN=$((WARN+1)); }
section() { echo ""; echo "── $1 ──"; }

section "A. 基础环境"
if command -v docker >/dev/null 2>&1; then
    ok "docker $(docker --version | grep -o '[0-9.]*' | head -1)"
else
    bad "docker 未安装：curl -fsSL https://get.docker.com | sh"
fi
if docker compose version >/dev/null 2>&1; then
    ok "compose v2 $(docker compose version | grep -oE 'v[0-9]+\.[0-9]+' | head -1)"
else
    bad "docker compose v2 不可用（新版 docker 自带；老版本需装 docker-compose-plugin）"
fi
if [ -f "$SCRIPT_DIR/.env" ]; then
    ok ".env 已存在"
    # 提醒改默认密码（不显示密码本身）
    grep -q "ChangeMe" "$SCRIPT_DIR/.env" && warn ".env 里还有 ChangeMe 占位密码，部署前务必改掉"
else
    warn ".env 不存在（cp .env.example .env 并填写；不启动隧道可暂不填 CF/TS 项）"
fi

section "B. 数据目录与磁盘"
if [ -d "$DATA_ROOT" ]; then
    FS_TYPE=$(stat -f -c %T "$DATA_ROOT" 2>/dev/null || echo unknown)
    case "$FS_TYPE" in
        ext2|ext3|ext4|xfs|btrfs|zfs|f2fs) ok "文件系统 $FS_TYPE（原生 Linux，适合 SQLite/Chroma）" ;;
        ntfs|fuseblk|cifs|nfs) bad "文件系统 $FS_TYPE —— SQLite 会随机锁死（第一轮部署文档的红线），换 ext4/xfs 路径" ;;
        *) warn "文件系统 $FS_TYPE 未识别，确认不是 NTFS/NFS 挂载即可" ;;
    esac
else
    warn "$DATA_ROOT 不存在（init.sh 会创建；若打算用别的路径，同步改 .env 的 DATA_ROOT）"
    FS_TYPE="（目录不存在，跳过文件系统检查）"
fi
FREE_GB=$(df -BG --output=avail "$(dirname "$DATA_ROOT" 2>/dev/null || echo /)" 2>/dev/null | tail -1 | tr -dc '0-9')
if [ -n "${FREE_GB:-}" ]; then
    if [ "$FREE_GB" -ge 25 ]; then
        ok "磁盘剩余 ${FREE_GB}G（qwen3:8b+bge-m3 约 7G，余量充足）"
    else
        warn "磁盘剩余 ${FREE_GB}G，模型+文档+备份建议 25G+"
    fi
fi

section "C. 端口占用（零端口方案应为空）"
for p in 80 443 6806 3001; do
    if ss -tlnp 2>/dev/null | grep -q ":$p "; then
        warn "端口 $p 已被占用 —— 本方案不需要任何监听端口，请确认不是旧服务残留"
    fi
done
ok "端口检查完成（如上方无 WARN 则干净）"

section "D. 隧道前置"
if [ -e /dev/net/tun ]; then
    ok "/dev/net/tun 存在（Tailscale 容器可用）"
else
    bad "/dev/net/tun 不存在：modprobe tun 并确保内核模块开机加载"
fi
if curl -s --max-time 8 -o /dev/null https://api.cloudflare.com; then
    ok "出站可达 Cloudflare（cloudflared 可用）"
else
    warn "Cloudflare 出站不通 —— 国内服务器常见，建议走 Tailscale 方案"
fi
if curl -s --max-time 8 -o /dev/null https://login.tailscale.com; then
    ok "出站可达 Tailscale 控制面"
else
    warn "Tailscale 控制面不可达（检查防火墙出站策略）"
fi

section "E. GPU（无 N 卡自动跳过）"
if command -v nvidia-smi >/dev/null 2>&1 && nvidia-smi >/dev/null 2>&1; then
    VRAM=$(nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits 2>/dev/null | head -1)
    ok "NVIDIA GPU 检测到（显存 ${VRAM}MiB）"
    if docker info 2>/dev/null | grep -qi nvidia; then
        ok "nvidia-container-toolkit 已接入 docker"
    else
        warn "检测到 GPU 但 nvidia-container-toolkit 未装：apt install nvidia-container-toolkit && 重启 docker"
    fi
else
    ok "无 NVIDIA GPU（CPU 推理可跑，qwen3:8b 约 5-15s/百字；记得开 compose 里 OLLAMA_KEEP_ALIVE）"
fi

section "F. 工具链"
command -v python3 >/dev/null 2>&1 && ok "python3 $(python3 --version | grep -o '[0-9.]*')" || warn "python3 缺失（服务器上跑 MCP/CLI 才需要；纯容器栈不需要）"
command -v curl >/dev/null 2>&1 && ok "curl 存在" || warn "curl 缺失（init.sh 健康检查用）"

# ---------- 汇总 ----------
echo ""
echo "══════════════════════════════════════"
echo "  检查完成：✅ $PASS 项通过　⚠️  $WARN 项提醒　❌ $FAIL 项必须处理"
if [ "$FAIL" -gt 0 ]; then
    echo "  处理完 ❌ 项后再执行：docker compose up -d"
    exit 1
else
    echo "  环境就绪，下一步：bash init.sh && docker compose up -d"
    exit 0
fi
