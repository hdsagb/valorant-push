# 无畏契约商店推送 · 服务器版

Docker 一键部署的商店推送服务：每天准点把国服商店截图推送到你的邮箱 / 企业微信，全程 HTTPS，手机电脑随时查看。

## 1. 简介

- **一句话定位**：无畏契约商店推送的服务器版——部署在自己的云服务器上，由服务端容器每天定时抓取商店并推送，无需个人电脑开机。
- **与个人版的关系**：同仓库内的两个独立包（`app/` 个人版、`server/` 服务器版），互不引用。账号凭证不支持在网页上扫码登录，一律由**个人版导出凭证包（`.vpcred`）→ 服务器版管理界面导入**。
- **R6 提醒**：同一账号凭证只在一端启用推送——导入服务器版后，请在个人版停止其定时推送。

## 2. 前置要求

- Linux 服务器（x86_64），已安装 Docker Engine 24+ 与 Docker Compose v2（`docker compose version` 可用）
- 使用 HTTPS 需要一个域名，其 DNS A 记录已指向本服务器公网 IP；没有域名可临时直连（章节 4）
- 云安全组 / 防火墙放行 **80、443**（Let's Encrypt 签发与续期证书必需）
- 默认形态（章节 3）下 80/443 需由本服务独占；同机已有统一反代（多个站点按域名分流）时，改用章节 5.1 的外部反代形态

## 3. 快速开始（主路径）

```bash
git clone https://github.com/hdsagb/valorant-push.git
cd valorant-push/server
docker compose pull && docker compose up -d   # 拉不动 GHCR 就用 up -d --build
docker compose logs -f push_server            # 首次启动日志里有「一次性设置码」
```

> 全新部署**无需创建 `.env`**（所有变量均可选）；仅需要固定 `IMAGE_TAG` / `VAL_SERVER_MASTER_KEY` 时才建（生成与说明见 `.env.example` 注释）。

**首次使用流程**（接上）：

1. 浏览器打开 `http://服务器IP`（绑定域名前走 HTTP 明文，仅初始化这一次）；
2. 输入日志中的一次性设置码（形如 `首次启动：请在管理界面输入一次性设置码完成初始化：xxxx`，错过可 `docker compose logs push_server | grep 设置码` 重看，初始化完成前重启不变）；
3. 设置管理密码，并**按需绑定域名**：
   - 默认形态（80/443 空闲）：填写域名（如 `push.example.com`），保存后首次 HTTPS 访问时内置 Caddy 自动签发证书，界面随即跳转 HTTPS；
   - 服务器上已有其它站点的反代：填写外部反代中已为本项目配置的域名（见章节 5.1）；
   - 无域名 / 直连 IP：留空（证书无法为裸 IP 签发）；
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

## 5. HTTPS / 域名绑定

**形态决策**：服务器 80/443 空闲 → 默认形态（章节 3，内置 Caddy 独占）；80/443 已被其它站点占用 → 外部反代形态（章节 5.1）。

默认形态采用 Caddy **on-demand TLS**：证书不预签发，首次 HTTPS 访问某域名时按需签发（首次握手多花几秒，属预期），续期全部自动。应用侧维护「已绑定域名」事实源（初始化向导写入，存于 `settings.json`）：Caddy 遇到未知域名时向应用 `GET /api/caddy-ask?domain=xxx` 征询许可，**只对已绑定域名放行**，其余一律 403——不会为其它域名签发证书或代理任何内容。绑定动作零侵入：不写已有配置文件、不抢占已被占用的端口（若误在有站点的服务器上全量 `up -d`，仅内置 caddy 因端口冲突自身启动失败，已有站点不受影响）。

- Caddy 自动续期证书，无需 certbot、无需 crontab；证书存放在 `caddy_data` 命名卷。
- Caddy 反代时自动附 `X-Forwarded-Proto: https`，服务端据此给会话 Cookie 加 `Secure` 属性（代码内置探测，无需配置）。
- 域名绑定后，浏览器以 `http://域名` 访问页面会自动 302 引导到 HTTPS（仅页面路径，不影响接口与证书签发）。
- 管理界面状态卡「绑定域名」可**一键自检**：服务端实测 `https://<域名>/healthz`（总超时 5 秒），按结果提示 DNS 未生效 / 证书未就绪 / 反代指向不对 / 超时。

**更换域名**（v0.4.1 暂不支持界面换绑，逃生通道如下，在 `server/` 目录执行）：

```bash
docker compose exec push_server python -c "import json,pathlib; p=pathlib.Path('/data/settings.json'); d=json.loads(p.read_text()); d['domain']='新域名'; p.write_text(json.dumps(d, ensure_ascii=False))"
docker compose restart push_server
```

旧域名的证书在到期前仍可能被继续服务，建议同步把 DNS 切到新域名。

### 5.1 同机已有反向代理（外部 Caddy 接管 80/443）

**适用判定：服务器 80/443 已被其它站点占用时用本形态**（例如一台 Caddy 用不同域名分流多个站点）。此形态为与默认形态并列的一等公民：TLS / 证书 / 按域名分流全部由外部反代负责，本项目**不启动**自带的 `caddy`、**不改动**用户已有反代的任何配置，已有站点不受影响。

1. 在 `server/` 目录新增 `docker-compose.override.yml`，只把应用端口发布到本机回环：

```yaml
services:
  push_server:
    ports:
      - 127.0.0.1:8787:8787
```

2. 只启动应用容器：`docker compose up -d push_server`。**不要**执行 `docker compose up -d`——那会拉起本项目自带的 `caddy`，与外部反代争抢 80/443 导致其启动失败。
3. 在外部反代的站点配置中**新增**一个站点块指向 `127.0.0.1:8787`（其它站点的配置原样不动）：

```caddyfile
你的域名 {
	reverse_proxy 127.0.0.1:8787
}
```

4. 初始化向导「绑定域名」填写**外部反代中已为本项目配置的域名**；保存后在管理界面状态卡点「检查域名」一键自检（服务端实测 HTTPS 可达性，报错按提示排查反代指向 / DNS / 证书）。

- 外部反代须附 `X-Forwarded-Proto: https`（Caddy 默认自带；Nginx 需手动加 `proxy_set_header`），否则会话 Cookie 不会带 `Secure`。
- 升级只动应用容器：`docker compose pull push_server` + `docker compose up -d push_server`。
- 回滚到「自带 Caddy 独占 80/443」：先停用外部反代，再注释掉 override 文件的 `ports`，最后 `docker compose up -d`。

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
- 外部反代形态（章节 5.1）只升级应用容器：`docker compose pull push_server` + `docker compose up -d push_server`。
- **0.4.0 → 0.4.1 注意**：Caddyfile / docker-compose.yml 已改为 on-demand TLS + ask 形态。`git pull && docker compose pull && docker compose up -d` 后需再执行 **`docker compose restart caddy`**（bind mount 挂载的 Caddyfile 内容变化不会自动重建容器）；重启前旧配置仍然有效，无中断窗口。升级后 serve 启动会把 `.env` 的 `DOMAIN` 自动种入设置（`settings.json` 的 `domain` 字段，向导/自检共用），确认已生效后即可从 `.env` 删除该变量。

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
| 绑定域名后首次 HTTPS 访问失败 / 卡住 | 多为 DNS 未生效或应用未就绪，稍后重试；`docker compose logs caddy` 可查 ask 征询记录 |
| 状态卡「检查域名」报 DNS 未生效 / 证书未就绪 | DNS 未生效：确认 A 记录已指向本服务器公网 IP 并等待生效；证书未就绪：确认安全组放行 443、反代已指向本项目，稍后重试（on-demand 证书在首次成功访问时签发） |

## 11. GHCR 包可见性

首次发布后，镜像包默认为**私有**，其他机器拉取需要 `docker login ghcr.io`。建议改为公开（镜像不含任何用户数据，仅代码产物）：

GitHub 仓库 → **Packages** → `valorant-push-server` → **Package settings** → **Danger Zone** → **Change visibility** → **Public**。

## 12. 安全自查清单（部署机人工核对）

> 对应《风险对策详解.md》R8 九项。前 8 项内生能力已由代码/镜像保证，此处给部署机逐条验证命令；带「运营侧」标注的项需在服务器上人工核对。自动化部分（日志与状态文件无明文凭证）见 `server/tests/test_security.py`。

| # | 条目 | 验证方式与预期 |
| --- | --- | --- |
| 1 | SSH 仅密钥登录 | 服务器上 `grep -E "^PasswordAuthentication" /etc/ssh/sshd_config` → `no`（运营侧） |
| 2 | 系统自动安全更新 | `systemctl is-active unattended-upgrades` → `active`（运营侧） |
| 3 | 容器非 root | `docker compose exec push_server id` → `uid=1000(app)` |
| 4 | data/ 权限 0700 | `docker compose exec push_server stat -c %a /data` → `700` |
| 5 | 端口仅 80/443 + SSH | 云安全组控制台逐条核对，无多余放行端口（运营侧） |
| 6 | Web/日志无明文凭证 | `python -m pytest server/tests/test_security.py -v` 全绿 + 管理界面走查（导入/渠道卡片不回显明文凭证，仅 `*_set` 标记） |
| 7 | data/ 定期备份 | 按章节 6 命令演练一次备份 + 恢复（运营侧） |
| 8 | 日志滚动生效 | `docker compose exec push_server ls /data` → 有 `app.log`，滚动后见 `app.log.1`（单文件 ≤ 2MB，共 3 份） |
| 9 | 升级不盲追 latest | `.env` 固定 `IMAGE_TAG`（见 `.env.example` 注释），升级前核对目标版本号（运营侧） |
