# 本地可视化调试指南（Windows + Docker Desktop）

> 用途：在自己的电脑上用浏览器打开项目看板与 SiYuan 笔记。

## 零、推荐路径：零依赖本地看板（不需要 Docker）

如果 Docker 出问题（空间不足 / snapshotter 损坏）或只想快速看图，用内置看板：

```bash
cd kb-stack
py kb_dashboard.py                    # 打开 http://127.0.0.1:8765
py kb_dashboard.py --port 9000        # 换端口
py kb_dashboard.py --no-ai            # 关闭 AI 问答（纯静态看板）
```

**看板提供**：
- 项目卡片墙：状态徽章（成品/活跃/搁置/未填写）、来源根目录、技术栈、最后活跃日期
- 模块计数：已完成 / 缺失 / 待办（自动解析复盘文档的 §2.2、§2.3、§4.1）
- 右下角 AI 面板：调本地 Ollama + ChromaDB 做检索问答（`仅检索` 秒回，`提问` 走大模型）

**特点**：零第三方依赖（仅标准库）、只绑定回环地址、离线可用。

---

## 一、Docker 方案（AnythingLLM + SiYuan）

```bash
cd kb-stack

# 启动（首次需拉取 ~5GB 镜像）
docker compose -f docker-compose.yml -f docker-compose.local.yml up -d siyuan anythingllm

# 查看状态 / 停止
docker compose -f docker-compose.yml -f docker-compose.local.yml ps
docker compose -f docker-compose.yml -f docker-compose.local.yml stop
```

| 服务 | URL | 凭据 |
|---|---|---|
| AnythingLLM（主力看板） | http://127.0.0.1:3001 | 首次进入创建管理员账号 |
| SiYuan（备用看板） | http://127.0.0.1:6806 | 锁屏密码见 `.env` 的 `SIYUAN_ACCESS_AUTH_CODE` |

### 已知故障与修复（实测）

| 故障 | 根因 | 解法 |
|---|---|---|
| SiYuan 反复重启（退出码 26） | `PUID=0` 在 v3.8.3 不兼容 | `.env` 用 `PUID=1000/PGID=1000` |
| SiYuan 报 `chmod ... operation not permitted` | Windows NTFS 不支持 Unix chmod | 用命名卷（`docker-compose.local.yml` 已配置） |
| SiYuan 显示 `unhealthy` 但能访问 | 健康检查用 wget，密码保护返回 401 被判失败 | 已在 compose 中改为接受 200/401 |
| 镜像解压报 `input/output error` | C 盘空间不足（<25GB）导致 overlayfs 写失败 | 清理 C 盘，或迁移 Docker 数据盘 |
| `docker ps` 报 `readdirent ... input/output error` | snapshotter 损坏 | 清空间后重启 Docker Desktop；仍失败则 Reset to factory defaults |


## 三、关键配置说明

**为什么需要 `docker-compose.local.yml`？**

主 `docker-compose.yml` 是零端口安全版（用于服务器 + 隧道），本机调试需要直连浏览器，故用覆盖层加回环端口。

**为什么 Ollama 地址是 `host.docker.internal`？**

本机 Ollama 是**原生安装在 Windows** 上（`ollama serve` 监听 11434），不在 Docker 网络内。容器要通过宿主网关访问它：

```yaml
OLLAMA_BASE_PATH: http://host.docker.internal:11434
extra_hosts:
  - "host.docker.internal:host-gateway"
```

上服务器部署时（Ollama 也在 compose 里），用主 compose 的 `http://ollama:11434`，不需要本文件。

**数据落盘位置**：`F:/kb-data/`（由 `.env` 的 `DATA_ROOT` 控制）

```
F:/kb-data/
├── siyuan/workspace/        # 思源笔记数据（纯文件，可直接备份）
└── anythingllm/storage/     # 向量库 + 文档缓存
```

## 四、故障排查

| 现象 | 原因与解法 |
|---|---|
| `failed to connect to the docker API` | Docker Desktop 未启动。运行 `Start-Process 'C:\Program Files\Docker\Docker\Docker Desktop.exe'` 后等 30-60 秒 |
| 容器起来但页面打不开 | 等 1-2 分钟（首次启动要初始化数据库）；`docker logs anythingllm --tail 50` 看进度 |
| AnythingLLM 报 Ollama 连接失败 | 确认 Windows 上 Ollama 在跑：`curl http://127.0.0.1:11434/api/tags`；若容器内连不上，检查 `.env` 的 `OLLAMA_BASE_PATH` 是否为 `host.docker.internal` |
| SiYuan 报权限错误 | 本机调试用 `PUID=0/PGID=0`（Docker Desktop 通常以 root 运行）；服务器上是 `1000:1000` |
| 端口 3001/6806 被占用 | `netstat -ano | findstr :3001` 查占用进程；改 `.env` 里映射端口或停掉冲突服务 |
| 镜像拉取慢/中断 | 重跑 `up` 命令即可续传（Docker 分层缓存）；国内可配 Docker Desktop 镜像加速器 |

## 五、与服务器部署的关系

| | 本机调试 | 服务器生产 |
|---|---|---|
| 启动命令 | 加 `-f docker-compose.local.yml` | 只用 `docker compose` |
| 端口 | 回环映射（仅本机可访问） | 零端口，走 Tailscale / Cloudflare 隧道 |
| Ollama 地址 | `host.docker.internal:11434`（原生安装） | `ollama:11434`（容器内） |
| 用户 | `PUID=0` | `PUID=1000`（与数据目录属主一致） |
