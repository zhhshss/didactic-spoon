# mambo-hachimi 自动签到脚本

给 `mambo-hachimi.biliblili.uk`（結束バンド - EMBY 管理平台）写的**每日自动签到**脚本。

> 仅用于**自己账号**的个人日常自动化。请遵守目标站点的用户协议。

---

## 这个脚本解决了什么难题

目标站点的**登录**与**签到**都强制开启了 **Cloudflare Turnstile v2** 人机验证
（实测 `/api/settings/verification/public` 里 `login`、`checkin` 均 `enabled:true`，分数阈值 0.8）。

普通自动化（含 Playwright headless）会被 Turnstile 识别为机器人并拒绝放行
（实测返回 `401 / failure_retry`）。因此本脚本改用 **SeleniumBase UC Mode**
（Undetected Chrome）——用**真实 Chrome + 反指纹**让浏览器表现得像真人，
配合**持久化 profile** 保留登录态与 Cloudflare 通行证，从而以正常方式通过验证。

> 这不是"破解"Cloudflare，而是让自动化浏览器以接近真人的方式正常完成验证。

---

## 实测结果（已端到端跑通 ✓）

在 Linux 容器（xvfb 虚拟显示）下实测，账号签到成功：

```
登录态有效 → 复用登录态，跳过登录
/dashboard 找到签到卡片 → 点击「立即签到」
签到成功 ✓ 今日已签到（货币单位：STARRY）
```

后端返回：`{"success":true,"hasCheckedInToday":true,"amount":,"currencyUnit":"STARRY"}`
余额 ` →  STARRY`（），签到卡片由「今天还没有签到哦」变为绿色「今日已签到」。
重复运行会识别"今日已签到"并立即退出（幂等，可安全配定时任务）。

---

## 安装

需要 **Python 3.9+** 与本机可用的 **Google Chrome**。

```bash
pip install -r requirements.txt
```

无桌面的 Linux 服务器还需安装虚拟显示组件：

```bash
sudo apt-get install -y xvfb scrot python3-tk xdotool
```

> SeleniumBase 首次运行会自动下载与 Chrome 匹配的驱动，无需手动处理。

---

## 配置凭据

复制模板并填写你的账号：

```bash
cp .env.example .env
# 然后编辑 .env，填入 MAMBO_USERNAME / MAMBO_PASSWORD
```

`.env` 已被 `.gitignore` 忽略，不会进入版本库。**切勿把密码硬编码进代码或提交到仓库。**

---

## 运行

```bash
python checkin.py
```

脚本会自动：

1. 复用持久化登录态（`.profile/`）；失效才重新登录
2. 查询今日是否已签到（已签到则直接退出，**可安全重复运行**）
3. 未签到 → 进入 `/dashboard` 点击「立即签到」→ 通过 Turnstile → 校验结果

---

## 显示模式（关键）

通过 `.env` 的 `MAMBO_DISPLAY` 控制（或同名环境变量）：

| 取值 | 适用场景 | 说明 |
|------|----------|------|
| `headed` | **本机有桌面（推荐）** | 弹出真实窗口，Turnstile 最易通过；必要时可手动点验证 |
| `xvfb` | 无桌面服务器 / NAS | 用虚拟显示，UC 仍可做 GUI 点击 |
| `headless` | 不推荐 | 纯无头多半过不了 Turnstile |
| `auto`（默认） | 自动判断 | 有 `$DISPLAY` 用 `headed`，否则用 `xvfb` |

> **首次登录建议用 `headed` 跑一次**：万一 Turnstile 需要交互，你可以在窗口里手动点一下。
> 成功后登录态与 Cloudflare 通行证会存进 `.profile/`，之后基本可全自动。

---

## 定时自动签到

### Linux / macOS（cron）

```bash
crontab -e
# 每天 9:07 自动签到（避开整点高峰），日志写入 checkin.cron.log
7 9 * * * cd /路径/到/项目 && /usr/bin/python3 checkin.py >> checkin.cron.log 2>&1
```

### Windows（任务计划程序）

新建任务 → 触发器设为每天 → 操作运行 `python C:\路径\checkin.py`。

> 定时任务依赖 `.profile/` 里的登录态。若某天 session 过期且需要交互验证，
> 该次会失败并在日志中提示——届时手动用 `headed` 模式跑一次刷新登录态即可。

### GitHub Actions（云端定时）

仓库已内置 `.github/workflows/checkin.yml`，可在 GitHub 云端每天定时签到（默认 **北京时间 09:07**）。

**配置步骤：**

1. 在仓库 **Settings → Secrets and variables → Actions** 新增两个机密（Repository secret）：
   - `MAMBO_USERNAME` —— 你的用户名
   - `MAMBO_PASSWORD` —— 你的密码
2. 进入 **Actions** 页面，对 `每日自动签到` 工作流点 **Run workflow** 手动触发一次，验证是否跑通。
3. 之后每天按 `cron` 自动执行。要改时间，编辑 workflow 里的 `cron`（注意它是 **UTC**：北京时间 = UTC + 8）。

工作流以 `xvfb` 虚拟显示运行 UC Mode，并用 `actions/cache` 持久化 `.profile/` 登录态跨运行复用。
脚本还会把登录令牌主动写入 `.profile/.mambo_auth_token` 落盘，确保缓存恢复后能稳定免登录复用。

> 💡 **可选：连首次登录都免掉**。在已登录的本机执行 `python checkin.py --export-token`，复制输出的令牌，
> 到 **Settings → Secrets and variables → Actions** 新增机密 `MAMBO_AUTH_TOKEN`。云端会优先用它注入登录态，
> 首跑即免登录。令牌约 7 天有效，过期后重新导出更新即可。

> ⚠️ **关于「反复登录」**：站点登录令牌（JWT）有效期约 **7 天且无自动续期**。
> Chromium 的 `localStorage` 是异步落盘的，仅靠浏览器缓存常 flush 不及时，导致缓存恢复后读不到令牌、
> 被迫每次重新登录（每次还会收到一封登录提醒邮件）。本脚本改为**在浏览器存活时主动把令牌落盘**并随缓存复用，
> 故 7 天内的运行都**免登录**；令牌自然过期后才自动重新登录一次刷新（此时会收到一封邮件，属正常）。
> 即登录频率已从「每天」降到「约每周一次」。

---

## 工作原理（基于对前端的逆向）

| 环节 | 接口 / 选择器 |
|------|---------------|
| 登录态校验 | `GET /api/auth/me`（带 `Authorization: Bearer <auth_token>`） |
| 登录 | `POST /api/auth/login`，前端在过验证后自动提交 |
| 登录态存储 | `localStorage.auth_token`（勾选「记住密码」时） |
| 签到状态 | `GET /api/checkin/status` → `{hasCheckedInToday, amount, ...}` |
| 执行签到 | 点击 `/dashboard` 的 `[data-checkin-card]`「立即签到」 |

脚本**以业务结果为最终判据**（登录态 / 签到状态），而非 Turnstile token 是否回填——
因为本站常常**无感通过**且 token 不暴露于 DOM。这样既准确又不会误判阻断。

---

## 文件说明

| 文件 | 作用 |
|------|------|
| `checkin.py` | 主签到脚本 |
| `requirements.txt` | Python 依赖 |
| `.env.example` | 配置模板（复制为 `.env` 使用） |
| `.env` | 你的实际凭据（**不提交**） |
| `.profile/` | 持久化浏览器数据（登录态、cf 通行证，**不提交**） |
| `.profile/.mambo_auth_token` | 脚本主动落盘的登录令牌，随 `.profile/` 缓存复用以免登录（**不提交**） |

---

## 常见问题

- **登录卡住 / 报"登录未成功"**：用 `MAMBO_DISPLAY=headed` 跑一次，在弹窗里手动完成验证。
- **数据中心 IP（云服务器）成功率低**：Cloudflare 对机房 IP 信誉评分低；家庭宽带成功率更高。
- **想换账号**：删除 `.profile/` 目录清除旧登录态，再改 `.env`。

---

## 免责声明

本脚本仅供学习与个人账号的日常自动化使用，使用者需自行遵守目标站点条款并承担相应风险。
