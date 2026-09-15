# 零公网端口部署 · 安全隧道方案（kb-stack）

> 目标：kb-stack 部署到 Linux 服务器后，**宿主机不开任何入站端口**。
> 所有对外访问经出站隧道建立（服务器只主动外连，不被动监听）。
> 对应 `docker-compose.yml` 零端口版（Caddy/SiYuan/AnythingLLM 均无 ports 映射）。

---

## 一、三种外网访问方案对比评估

| 评估维度 | 方案 A：Cloudflare Tunnel | 方案 B：Tailscale 私有组网 | 方案 C：Tailscale（内网直连）+ Cloudflare（临时分享）并用 |
|---|---|---|---|
| **配置复杂度** | 中。域名 NS 须托管在 CF；建 Tunnel→拷 Token→面板配 2 条路由→（建议）配 Access 门禁，全图形化约 20 分钟 | 低。注册 tailnet→生成 authkey→容器起来即用；管理员在后台点两次 approve（出口节点/子网路由） | 中高。两套都要配，但互不干扰；一次配好后日常零维护 |
| **网络延迟** | 较高。流量必经 CF 边缘节点（国内回源线路不稳时可能 200ms+，SSE 流式回答可能感知到首字延迟） | 最低。WireGuard 打洞成功后 P2P 直连（同机房/同城 <10ms）；打洞失败走 DERP 中继（延迟增加但可用） | 取决于走哪条路：日常用 Tailscale 直连（最快），分享链接走 CF（可接受） |
| **安全性** | 高，但攻击面是公网。域名暴露在公网，依赖 CF Access 门禁（邮箱验证码）+ 应用层密码双层防护；受益于 CF 的 WAF/DDoS 清洗；隐藏源站 IP | 最高。tailnet 外完全不可见——没有公网域名、没有暴露面，连接靠 WireGuard 身份认证（设备级），未经授权的设备根本不知道服务存在 | 分层最优。自己走设备级认证通道，他人临时访问走 CF 门禁；任一隧道被攻破都不影响另一条 |
| **是否依赖公网域名** | **依赖**。必须有 NS 托管在 Cloudflare 的域名（免费版可用） | **不依赖**。设备通过 100.x.x.x 的 tailnet IP 或 MagicDNS 名称互访 | 部分依赖（仅 CF 路径需要；Tailscale 路径不需要） |
| 适合场景 | 需要把问答页分享给协作者/手机随手开浏览器访问 | 纯个人使用、多设备（含手机装 Tailscale 客户端） | 推荐的最终形态：日常自己用 B，偶尔分享用 A |

**结论建议**：个人知识库 99% 的访问是你自己——**Tailscale 为主**（延迟低、零暴露面、不花钱不花域名）；只在需要把 RAG 问答页临时分享给别人时叠加 Cloudflare Tunnel。两个 profile 相互独立，可随时单独启停。

---

## 二、Cloudflare Tunnel 方案（compose profile: cf）

### 2.1 准备（Cloudflare 面板，约 10 分钟）

1. 域名 NS 托管到 Cloudflare（免费版即可）
2. **Zero Trust → Networks → Tunnels → Create a tunnel** → 选 Cloudflared → 命名 `kb-stack` → 复制 Token
3. Token 填入 `.env` 的 `CLOUDFLARE_TOKEN=<YOUR_CLOUDFLARE_TUNNEL_TOKEN_HERE>`
4. 同一页面配置 **Public Hostname**（两条）：

| Subdomain | Domain | Service | 说明 |
|---|---|---|---|
| kb | example.com | **HTTP://caddy:80** | 走 Caddy 按 Host 分流到 SiYuan |
| rag | example.com | **HTTP://caddy:80** | 同上，分流到 AnythingLLM |

   （都指向 caddy:80，由 Caddyfile 里 `http://kb.example.com` / `http://rag.example.com` 两个站点块按 Host 头分流）

5. **强烈建议加门禁**：Zero Trust → Access → Applications → Self-hosted → 域名 `kb.example.com`（和 `rag.example.com` 各一条）→ Policy: Allow → Emails 填你的邮箱。效果：任何人打开域名先过 Cloudflare 邮箱验证码，通过后才能见到应用登录页。

### 2.2 启动与验证

```bash
cd kb-stack
docker compose --profile cf up -d
docker logs -f cloudflared        # 看到 "Registered tunnel connection" 即成功
```

浏览器访问 `https://kb.example.com` → 过 Access 验证码 → SiYuan 锁屏密码 → 进入。
`https://rag.example.com` 同理进 AnythingLLM。

### 2.3 已并入 compose 栈的配置（摘录）

```yaml
  cloudflared:
    image: cloudflare/cloudflared:latest
    container_name: cloudflared
    restart: unless-stopped
    profiles: [cf]
    command: tunnel --no-autoupdate run --token ${CLOUDFLARE_TOKEN}
    networks: [kb]
    depends_on:
      - caddy
```

要点：`--no-autoupdate` 防止容器内自更新替换二进制（更新走换镜像 tag）；`profiles: [cf]` 使其默认不启动，`--profile cf` 才拉起；无任何端口映射——它是纯出站连接。

---

## 三、Tailscale 方案（compose profile: ts）

### 3.1 准备（约 5 分钟）

1. 注册 tailnet：https://login.tailscale.com （免费版 3 用户 100 设备）
2. **Settings → Keys → Generate auth key**：勾选 Reusable（可复用），建议勾 Ephemeral=否 → 得到形如 `tskey-auth-<RANDOM>` 的密钥
3. 填 `.env`：
   ```
   TS_AUTHKEY=<YOUR_TAILSCALE_AUTH_KEY_HERE>
   TS_HOSTNAME=kb-server
   TS_EXTRA_ARGS=--advertise-exit-node
   ```
4. 手机/笔记本安装 Tailscale 客户端并登录同一 tailnet

### 3.2 启动与直连访问

```bash
docker compose --profile ts up -d
docker logs -f tailscale          # 首次会打印一个登录 URL（用 authkey 则直接注册成功）
docker exec tailscale tailscale status   # 应显示 kb-server 及其 100.x.x.x 地址
```

访问方式（**Tailscale 容器 = 单目标代理模式**，`TS_DEST_IP: caddy` 把进入流量转给 Caddy）：

| 入口 | 打开的地址 | 落到哪 |
|---|---|---|
| 知识库 | `http://kb-server:8080`（MagicDNS 名）或 `http://100.x.x.x:8080` | Caddy :8080 → SiYuan |
| RAG 问答 | `http://kb-server:8081` | Caddy :8081 → AnythingLLM |

流量全程 WireGuard 加密，所以内网这段走 HTTP 也安全；无需任何域名和证书。

### 3.3 Exit Node 级别配置（把服务器当成外出入口）

「出口节点」= 你手机/笔记本在外面上网时，**所有流量**经 WireGuard 隧道从这台服务器出去（等效随身 VPN）。容器已通过 `TS_EXTRA_ARGS=--advertise-exit-node` 通告，还需两步：

1. **管理台批准**：login.tailscale.com → Machines → 找到 `kb-server` → `...` 菜单 → **Edit route settings** → 勾选 **Use as exit node** → Save
2. **客户端启用**：手机 Tailscale App → 出口节点选 `kb-server`；Windows/Mac 客户端菜单里同样选择

验证：手机开出口节点后访问 https://ifconfig.me ，显示的应是服务器 IP。

**子网路由（进阶，可选）**：若还想在 tailnet 里直达服务器所在局域网的其他设备（如打印机、路由器管理页），`.env` 里设 `TS_ROUTES=192.168.1.0/24`（按实际网段），重启 ts profile，再到管理台同一位置 approve 该 route；访问端开启 "Use subnet routes"。注意 Tailscale 子网路由默认开启 SNAT（来源 IP 会被改写成路由器地址，官方文档 kb/1019），个人场景通常无感。

### 3.4 关键设计说明

- `network_mode: host` 是**通告出口节点/子网路由的官方要求**（tailscaled 要写宿主机路由表）——这不是开放端口：Tailscale 不监听任何公网入站端口，连接由出站打洞建立
- `TS_STATE_DIR` 指到持久卷 `${DATA_ROOT}/tailscale`，重启容器不丢登录态
- `TS_DEST_IP: caddy` 是官方支持的单目标代理模式：tailnet 内访问本节点任意端口都会被转给 caddy——配合 Caddyfile 的 :8080/:8081 两个直达站点，避免依赖 Host 头

---

## 四、运维速查

```bash
docker compose ps                          # 查看业务栈（4 容器）
docker compose --profile cf --profile ts up -d   # 全量启动（业务+双隧道）
docker compose --profile cf restart cloudflared   # 重启某隧道
docker logs -f cloudflared                 # 隧道健康（看 Registered connection）
docker exec tailscale tailscale status     # tailnet 状态
```

排障：

| 症状 | 处理 |
|---|---|
| cloudflared 日志报 403/invalid token | `.env` 的 CLOUDFLARE_TOKEN 没拷全（要整段 eyJ 开头的串），改后 `docker compose --profile cf up -d --force-recreate` |
| 域名 502 Bad Gateway | 面板里 Service 应为 `http://caddy:80` 而非 `localhost`；确认 Caddyfile 里站点域名与 CF 路由域名一致 |
| Tailscale 容器反复重启 | 宿主机缺 TUN：`ls /dev/net/tun`，不存在则内核模块 `modprobe tun`；或检查 authkey 是否过期 |
| 手机连上 tailnet 但打不开 8080 | 用 `tailscale status` 确认 kb-server 在线；确认访问的是 MagicDNS 名或 100.x IP，不是局域网 IP |
| RAG 页面回答转圈后断开 | 隧道路径下 Caddy 已设 600s 超时+关闭缓冲；若仍断，检查 `.env` 的 OLLAMA_RESPONSE_TIMEOUT 是否足够（CPU 推理建议 12000000） |

## 五、防火墙收尾（可选加固）

宿主机若有 ufw/firewalld，保持默认拒绝入站即可（本方案不需要任何放行）。云服务器（阿里云/腾讯云）的安全组**全部关闭入站**——隧道是出站的，不受影响；SSH 建议改走 Tailscale（安全组关掉 22 后，`ssh user@kb-server` 经 tailnet 直达），至此服务器在公网上完全不可探测。
