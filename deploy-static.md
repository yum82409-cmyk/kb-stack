# 静态看板部署指南（Static Dashboard Deployment）

> 把项目卡片墙发布到静态托管平台。产物是**单个 `index.html`**（14KB，零外部依赖，完全离线可用）。
> AI 问答无法上静态平台（需要 Ollama 常驻进程）——继续用本机 `py kb_dashboard.py`。

## 构建产物

```bash
cd kb-stack
py build_static.py                # 输出 ./dist/index.html
py build_static.py -o ./public    # Netlify 惯例目录
```

**隐私提醒**：静态站是公开的，构建时脚本会自动扫描本机路径/密钥类敏感内容并警告。确认 `kb-docs/` 里的复盘文档可以公开后再发布。

---

## 平台一：Vercel（推荐，免费）

```bash
# 方式 A：CLI 直传（最快）
npm i -g vercel
cd kb-stack
py build_static.py -o ./dist
vercel dist --prod

# 方式 B：连接 Git 仓库（自动构建）
#   vercel.json 放在仓库根目录：
```

```json
{
  "buildCommand": "python build_static.py -o dist",
  "outputDirectory": "dist"
}
```

> Vercel 的构建环境是 Linux，`python build_static.py` 里的 chromadb 不需要（静态构建只解析 Markdown，零第三方依赖）。

## 平台二：Netlify

```bash
npm i -g netlify-cli
py build_static.py -o ./public
netlify deploy --dir=public --prod
```

或网页端：**Add new site → Deploy manually** → 把 `dist/` 文件夹拖进去。

## 平台三：Cloudflare Pages

```bash
npm i -g wrangler
py build_static.py -o ./dist
wrangler pages deploy dist --project-name=kb-stack
```

或网页端：**Create project → Direct Upload** → 拖入 `dist/`。

## 平台四：GitHub Pages

```bash
# 把 dist 推到 gh-pages 分支
py build_static.py -o ./dist
git subtree push --prefix dist origin gh-pages

# 或用 gh CLI：
git switch -c gh-pages
git checkout main -- dist
mv dist/* . && git add -A && git commit -m "deploy: static dashboard"
git push origin gh-pages
# 仓库 Settings → Pages → Source 选 gh-pages 分支
```

---

## 如果你有自己的域名

| 平台 | 域名配置位置 |
|---|---|
| Vercel | Project → Settings → Domains → 添加域名 → 按提示加 CNAME |
| Netlify | Site settings → Domain management → Add custom domain |
| CF Pages | Custom domains → 连接（域名已在 CF 则零配置） |
| GH Pages | 仓库 `dist/CNAME` 文件写入你的域名 |

## 与本机看板的关系

| | 本机看板（kb_dashboard.py） | 静态看板（build_static.py） |
|---|---|---|
| AI 问答 | ✅ 本地 Ollama + ChromaDB | ❌（平台无法跑模型） |
| 数据 | 实时读 kb-docs/ | 构建时烘焙，需重新构建更新 |
| 访问 | 仅 127.0.0.1 | 公网可访问 |
| 更新方式 | 刷新页面 | 改文档 → `py build_static.py` → 重新部署 |

**推荐工作流**：日常在本机看板用 AI 审阅 → 阶段性成果 `py build_static.py` → 部署到网站对外展示。

## 已有网站整合

如果你想把看板**嵌进现有网站**（而不是独立子站）：

```html
<!-- 方式一：iframe 嵌入（最简单） -->
<iframe src="https://你的看板域名/" style="width:100%;height:900px;border:none;"></iframe>

<!-- 方式二：直接把 dist/index.html 的 <style> 和 <main> 部分拷进你现有页面 -->
```
