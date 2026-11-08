# 无畏契约商店推送 · 服务器版

Docker 一键部署的商店推送服务：每天准点把国服商店截图推送到你的邮箱 / 企业微信，全程 HTTPS，手机电脑随时查看。

## 1. 简介

- **一句话定位**：无畏契约商店推送的服务器版——部署在自己的云服务器上，由服务端容器每天定时抓取商店并推送，无需个人电脑开机。
- **与个人版的关系**：同仓库内的两个独立包（`app/` 个人版、`server/` 服务器版），互不引用。账号凭证不支持在网页上扫码登录，一律由**个人版导出凭证包（`.vpcred`）→ 服务器版管理界面导入**。
- **R6 提醒**：同一账号凭证只在一端启用推送——导入服务器版后，请在个人版停止其定时推送。

## 2. 前置要求

- Linux 服务器（x86_64），已安装 Docker Engine 24+ 与 Docker Compose v2（`docker compose version` 可用）
- 一个域名，其 DNS A 记录已指向本服务器公网 IP
- 云安全组 / 防火墙放行 **80、443**（Caddy 签发与续期 HTTPS 证书必需）

## 3. 快速开始（主路径）

```bash
git clone https://github.com/hdsagb/valorant-push.git
cd valorant-push/server
cp .env.example .env && nano .env          # 填 DOMAIN
docker compose pull && docker compose up -d   # 拉不动 GHCR 就用 up -d --build
docker compose logs -f push_server         # 首次启动日志里有「一次性设置码」
```

**首次使用流程**（接上）：

1. 浏览器打开 `https://你的域名`；
2. 输入日志中的一次性设置码（形如 `首次启动：请在管理界面输入一次性设置码完成初始化：xxxx`，错过可 `docker compose logs push_server | grep 设置码` 重看，初始化完成前重启不变）；
3. 设置管理密码，进入管理界面；
4. 在个人版「导出凭证包」生成 `.vpcred` 文件，回到服务器版在「导入账号」卡片导入；
5. 配置推送渠道（邮件 / 企业微信）；
6. 开启每日定时。

## 4. 过渡直连形态（无域名，备选）

没有域名或暂不想走 HTTPS 时，可以临时直连：

1. 编辑 `docker-compose.yml`，取消 `push_server` 服务下 `ports:` 与 `- "8787:8787"` 两行注释；
2. `docker compose up -d push_server`（仅启动应用容器，不启动 Caddy）；
3. 浏览器访问 `http://服务器IP:8787`。

> **⚠️ 该形态下管理口令与凭证经明文 HTTP 传输，仅限内网或极短期过渡使用。**
>
> **⚠️ 必须在安全组把 8787 端口限制为你的来源 IP，严禁对公网开放。**

恢复默认形态：重新注释 ports 两行，`docker compose up -d` 全量启动，HTTPS 自动生效。

## 5. HTTPS / Caddy 说明

- Caddy 自动签发并续期 Let's Encrypt 证书，无需 certbot、无需 crontab；证书存放在 `caddy_data` 命名卷。
- Caddy 反代时自动附 `X-Forwarded-Proto: https`，服务端据此给会话 Cookie 加 `Secure` 属性（代码内置探测，无需配置）。
- 更换域名：改 `.env` 里的 `DOMAIN`，然后 `docker compose up -d caddy` 即可，证书对新域名自动重新签发。

## 6. 数据与备份

**所有运行数据都在命名卷 `valorant-push_push_data`**（挂载到容器 `/data`），内含：加密凭证 `accounts.json`、推送设置 `settings.json`、登录态 `auth.json`、加密主密钥 `master.key`、日志 `app.log`（滚动 2MB × 3 份）。容器可以随意删除重建，数据不丢；删卷才会丢数据。

一键备份 / 恢复（在 `server/` 目录执行）：

```bash
# 备份（生成到当前目录）
docker run --rm -v valorant-push_push_data:/data -v "$PWD:/backup" alpine \
  tar czf /backup/push-data-$(date +%Y%m%d).tgz -C /data .

# 恢复（空卷或损坏后）
docker run --rm -v valorant-push_push_data:/data -v "$PWD:/backup" alpine \
  sh -c "rm -rf /data/* && tar xzf /backup/push-data-YYYYMMDD.tgz -C /data"
```

- **master.key 丢失 = 服务端加密数据不可解密**（凭证密文、渠道密码全部报废）：需要删卷重置并重新从个人版导入凭证包。替代方案是在 `.env` 中固定 `VAL_SERVER_MASTER_KEY`（32 字节标准 base64，生成命令见 `.env.example` 注释），固定后换服务器也只需带同一个密钥。
- 建议把备份文件下载到服务器之外（对象存储 / 本地），服务器磁盘损坏时才有得恢复。

## 7. 安全基线清单

| 项 | 状态 |
| --- | --- |
| 非 root 容器运行（镜像内置 uid 1000 用户） | ✓ 内生已满足 |
| 数据目录权限（容器内 `/data` 启动时收紧为 0700） | ✓ 内生已满足 |
| HTTPS 默认形态 + Secure Cookie | ✓ 内生已满足 |
| SSH 密钥登录、禁用密码登录 | □ 运营侧 |
| 系统与 Docker 定期补丁 | □ 运营侧 |
| 安全组 443 可限源（仅自己常用网络时建议收紧） | □ 运营侧 |
| 管理口令不弱（≥12 位、不与其他站点复用） | □ 运营侧 |
| 凭证包导入完成后删除本地导出文件 | □ 运营侧 |

## 8. 升级

```bash
git pull
docker compose pull        # 或 docker compose build
docker compose up -d
```

- 数据在命名卷中保留，升级不丢数据；**升级前建议先按章节 6 备份**。
- 会话仅存内存，升级重启后需重新登录管理界面。
- 建议在 `.env` 中固定 `IMAGE_TAG`（如 `0.4.0`）而非盲追 `latest`，升级时按版本号平滑推进。

## 9. 维护说明

- 腾讯商店接口属未公开接口，存在变更可能；变更时需个人版 / 服务器版**双版本同步修复**（接口层为 vendored 脚本整文件拷贝机制，见 `push_server/vendor/` 各文件头部标注）。
- 日志查看：`docker compose logs -f push_server`，或命名卷内 `/data/app.log`（滚动 2MB × 3 份）。
- 推送渠道：邮件（SMTP）与企业微信自建应用；企业微信要求把服务器出口 IP 加入「企业可信 IP」，IP 校验失败（60020）时自动回退邮件渠道（需已配置邮件）。

## 10. 常见问题

| 现象 | 处理 |
| --- | --- |
| `docker compose pull` 拉取 GHCR 失败 | 改用源码构建 `docker compose up -d --build`，或为 Docker 配置镜像加速 |
| 推送图片中文显示为方块 | 确认镜像版本 ≥ 0.4.0（CJK 字体已内置） |
| 推送时间不准 | 确认未被覆盖 `TZ=Asia/Shanghai`（compose 已内置，勿删） |
| 企业微信 60020（可信 IP 校验失败） | 程序自动改用邮件渠道；或在企微后台把当前出口 IP 加入白名单 |

## 11. GHCR 包可见性

首次发布后，镜像包默认为**私有**，其他机器拉取需要 `docker login ghcr.io`。建议改为公开（镜像不含任何用户数据，仅代码产物）：

GitHub 仓库 → **Packages** → `valorant-push-server` → **Package settings** → **Danger Zone** → **Change visibility** → **Public**。
