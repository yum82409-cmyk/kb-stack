#!/usr/bin/env bash
# ============================================================
# backup.sh —— kb-stack 知识库冷备份（含轮转）
# ============================================================
# 备份内容（按价值排序，全部为纯文件，tar 冷备可直接恢复）：
#   1. kb-docs/            复盘文档（最核心资产，~50KB）
#   2. chroma_data/        向量索引（丢了可重建，但重建要分钟级）
#   3. templates/          录入模板
#   4. kb-docs-backup/     历史备份（AI 填写前的原文）
#   5. Linux 服务器：$DATA_ROOT 下的 siyuan workspace（若存在则一并备份）
#
# 轮转策略：保留最近 14 份，更老的自动删除（个人知识库日备足够）
#
# 用法：
#   bash backup.sh                    # 默认备份到 ./backups
#   DEST=/mnt/nas/kb bash backup.sh   # 备份到 NAS 挂载点等外部位置
#
# crontab 每日 04:30 自动备份（Linux 服务器）：
#   30 4 * * * cd /srv/kb-stack && bash backup.sh >> backups/backup.log 2>&1
#
# Windows 计划任务（本机）：
#   schtasks /create /tn "kb-backup" /tr "bash <REPO_PATH>/backup.sh" /sc daily /st 04:30
# ============================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEST="${DEST:-$SCRIPT_DIR/backups}"
STAMP="$(date +%Y%m%d_%H%M%S)"
KEEP=14

mkdir -p "$DEST"

# ---------- 收集存在的目录（缺哪个跳哪个，不报错）----------
ITEMS=()
for d in kb-docs chroma_data templates kb-docs-backup; do
    [ -d "$SCRIPT_DIR/$d" ] && ITEMS+=("$d")
done
# Linux 服务器场景：SiYuan 工作区（DATA_ROOT 默认 /srv/kb，可用环境变量覆盖）
DATA_ROOT="${DATA_ROOT:-/srv/kb}"
if [ -d "$DATA_ROOT/siyuan/workspace" ]; then
    ITEMS+=("$DATA_ROOT/siyuan/workspace")
fi

if [ ${#ITEMS[@]} -eq 0 ]; then
    echo "[错误] 没有找到任何可备份目录（当前目录是否正确？）"
    exit 1
fi

echo "==> 备份内容: ${ITEMS[*]}"
ARCHIVE="$DEST/kb-backup.$STAMP.tar.gz"
tar -czf "$ARCHIVE" -C "$SCRIPT_DIR" "${ITEMS[@]/#\//}" 2>/dev/null \
  || tar -czf "$ARCHIVE" -C "$(dirname "$DATA_ROOT")" "${ITEMS[@]}"  # 绝对路径兜底
SIZE=$(du -h "$ARCHIVE" | cut -f1)
echo "✅ 已生成 $ARCHIVE ($SIZE)"

# ---------- 轮转：保留最近 $KEEP 份 ----------
cd "$DEST"
ls -1t kb-backup.*.tar.gz 2>/dev/null | tail -n +$((KEEP + 1)) | while read -r old; do
    rm -f "$old"
    echo "  轮转删除: $old"
done

echo "✅ 备份完成，当前保留 $(ls -1 kb-backup.*.tar.gz | wc -l) 份（策略：最近 $KEEP 份）"
