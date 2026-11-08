"""本地设置向导（只监听 127.0.0.1，不需要公网 IP / 域名 / 服务器）。"""
from __future__ import annotations

import asyncio
import base64
import logging
import os
import secrets
import sys
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from aiohttp import web

from . import __version__, accounts, config, credpack, login, paths, pipeline, scheduler
from .channels import ChannelError
from .channels import email as email_channel
from .channels import wecom as wecom_channel
from .models import zone

logger = logging.getLogger(__name__)

HOSTS = ("127.0.0.1", "localhost", "::1")
PORT_CANDIDATES = range(8787, 8808)

# 每次启动随机生成，页面通过 <meta name="csrf-token"> 拿到；写接口必须带上它
CSRF_TOKEN = secrets.token_urlsafe(32)
CSRF_HEADER = "X-CSRF-Token"
REQUESTED_WITH = "ValorantPushPersonal"
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

routes = web.RouteTableDef()


def _origin_allowed(origin: str) -> bool:
    """允许省略 Origin（非浏览器请求），但带了就必须是本机页面。"""
    if not origin:
        return True
    try:
        parsed = urlsplit(origin)
    except ValueError:
        return False
    return parsed.scheme in {"http", "https"} and parsed.hostname in HOSTS


@web.middleware
async def local_only(request: web.Request, handler):
    """只接受本机页面发起的请求：校验 Host / Origin / Sec-Fetch-Site + 写接口令牌。"""
    host = (request.host or "").split(":")[0].strip("[]")
    if host not in HOSTS:
        return web.Response(status=403, text="local access only")
    if not _origin_allowed(request.headers.get("Origin", "")):
        return web.Response(status=403, text="cross-site request blocked")
    fetch_site = request.headers.get("Sec-Fetch-Site", "")
    if fetch_site and fetch_site not in {"same-origin", "none"}:
        return web.Response(status=403, text="cross-site request blocked")
    if request.method not in SAFE_METHODS:
        token = request.headers.get(CSRF_HEADER, "")
        if (
            request.headers.get("X-Requested-With") != REQUESTED_WITH
            or not token
            or not secrets.compare_digest(token, CSRF_TOKEN)
        ):
            return web.Response(status=403, text="invalid request token")
    return await handler(request)


def _masked_status() -> dict[str, Any]:
    settings = config.load_settings()
    state = config.load_state()
    account_list = accounts.summary()
    account_count = len(account_list)
    expired_names = [item["name"] for item in account_list if item["auth_expired"]]
    logged_in = account_count > 0
    auth_expired = bool(account_count) and len(expired_names) == account_count
    single = account_list[0] if account_count == 1 else None

    next_push = scheduler.next_run(settings, state)
    now = datetime.now(zone())
    if next_push is None:
        next_run_text = ""
    elif not logged_in:
        next_run_text = "登录后开始"
    elif state["last_push_date"] != now.strftime("%Y-%m-%d") and next_push <= now:
        next_run_text = "即将补推"
    else:
        next_run_text = next_push.strftime("%m-%d %H:%M")
    return {
        "version": __version__,
        "logged_in": logged_in,
        "login_type": (single["login_type"] if single else ("multi" if account_count > 1 else "")),
        "user_id_masked": single["user_id_masked"] if single else "",
        "accounts": account_list,
        "account_count": account_count,
        "account_limit": accounts.MAX_ACCOUNTS,
        "expired_accounts": expired_names,
        "auth_expired": auth_expired,
        "channel": settings["channel"],
        "email_configured": config.email_configured(settings),
        "wecom_configured": config.wecom_configured(settings),
        "config_problems": config.validate(settings),
        "schedule_enabled": settings["schedule_enabled"],
        "push_time": f"{settings['push_hour']:02d}:{settings['push_minute']:02d}",
        "next_run": next_run_text,
        "last_push_at": state["last_push_at"],
        "last_push_ok": bool(state["last_push_ok"]),
        "last_channel": state["last_channel"],
        "last_error": state["last_error"],
        "last_summary": state["last_summary"],
        "data_dir": str(paths.DATA_DIR),
        "autostart": _autostart_state(),
        "frozen": bool(getattr(sys, "frozen", False)),
        "login": (login.current().snapshot() if login.current() else None),
    }


def _autostart_state() -> bool:
    from . import autostart

    try:
        return autostart.is_enabled()
    except Exception:
        return False


@routes.get("/")
async def index(_: web.Request) -> web.Response:
    html = INDEX_HTML.replace("__CSRF_TOKEN__", CSRF_TOKEN)
    return web.Response(
        text=html,
        content_type="text/html",
        charset="utf-8",
        headers={"Cache-Control": "no-store"},
    )


@routes.get("/api/status")
async def api_status(_: web.Request) -> web.Response:
    return web.json_response(_masked_status())


@routes.get("/api/accounts")
async def api_accounts(_: web.Request) -> web.Response:
    items = accounts.summary()
    return web.json_response(
        {
            "ok": True,
            "accounts": items,
            "count": len(items),
            "limit": accounts.MAX_ACCOUNTS,
        }
    )


@routes.post("/api/accounts/rename")
async def api_accounts_rename(request: web.Request) -> web.Response:
    body = await request.json()
    account = accounts.rename(str(body.get("id") or ""), str(body.get("name") or ""))
    if not account:
        return web.json_response({"ok": False, "message": "账号不存在，可能已被解绑"}, status=404)
    return web.json_response(
        {"ok": True, "message": f"已改名为「{account['name']}」", "name": account["name"]}
    )


@routes.post("/api/accounts/remove")
async def api_accounts_remove(request: web.Request) -> web.Response:
    body = await request.json()
    account_id = str(body.get("id") or "")
    if not accounts.remove(account_id):
        return web.json_response({"ok": False, "message": "账号不存在，可能已被解绑"}, status=404)
    remaining = accounts.count()
    if remaining == 0:
        config.update_state(auth_expired=False)
    return web.json_response(
        {"ok": True, "message": "已解绑该账号", "count": remaining}
    )


@routes.post("/api/credentials/export")
async def api_credentials_export(request: web.Request) -> web.Response:
    body = await request.json()
    try:
        password = credpack.validate_password(str(body.get("password") or ""))
    except credpack.CredpackError as exc:
        return web.json_response({"ok": False, "message": str(exc)}, status=400)

    rows = accounts.all()
    payload = credpack.payload_accounts(rows)
    if not payload:
        return web.json_response(
            {"ok": False, "message": "没有可导出的账号，请先扫码登录绑定账号"}, status=409
        )
    skipped = len(rows) - len(payload)
    if skipped:
        logger.info("导出凭证包：跳过 %s 个凭证残缺的账号", skipped)
    content = credpack.build_pack(payload, password)
    filename = credpack.pack_filename()
    # 口令与凭证内容绝不落日志；日志只写文件名与账号数量
    logger.info("已生成凭证包 %s（%s 个账号）", filename, len(payload))
    return web.json_response(
        {
            "ok": True,
            "filename": filename,
            "content_b64": base64.b64encode(content).decode("ascii"),
        }
    )


@routes.get("/api/settings")
async def api_settings(_: web.Request) -> web.Response:
    return web.json_response(config.mask_settings(config.load_settings()))


@routes.post("/api/settings")
async def api_save_settings(request: web.Request) -> web.Response:
    patch = await request.json()
    updated = config.apply_update(config.load_settings(), patch)
    saved = config.save_settings(updated)
    return web.json_response(
        {
            "ok": True,
            "message": "已保存",
            "settings": config.mask_settings(saved),
            "problems": config.validate(saved),
        }
    )


@routes.post("/api/test-channel")
async def api_test_channel(request: web.Request) -> web.Response:
    body = await request.json()
    channel = "wecom" if body.get("channel") == "wecom" else "email"
    draft = body.get("draft") or {}
    settings = config.normalize(config.apply_update(config.load_settings(), draft))

    problems = config.validate({**settings, "channel": channel})
    if problems:
        return web.json_response(
            {"ok": False, "message": "；".join(problems)}, status=400
        )

    if channel == "email":
        # 防止页面被构造后把邮件凭据发到任意主机：只允许已保存的服务器或常见邮箱服务商
        host = settings["email"]["smtp_host"].strip().lower()
        saved_host = config.load_settings()["email"]["smtp_host"].strip().lower()
        known_hosts = {item[0] for item in config.SMTP_GUESS.values()}
        if host != saved_host and host not in known_hosts:
            return web.json_response(
                {
                    "ok": False,
                    "message": "测试只支持常见邮箱服务商或已保存的 SMTP 服务器；自定义服务器请先保存设置再测试",
                },
                status=400,
            )

    try:
        if channel == "wecom":
            await wecom_channel.send_test(settings)
        else:
            await email_channel.send_test(settings)
    except ChannelError as exc:
        payload: dict[str, Any] = {"ok": False, "message": exc.message, "code": exc.code}
        if channel == "wecom" and exc.code == 60020:
            payload["can_fallback"] = config.email_configured(settings)
        return web.json_response(payload, status=400)
    except Exception as exc:
        logger.exception("渠道测试失败")
        return web.json_response({"ok": False, "message": str(exc)}, status=500)

    return web.json_response({"ok": True, "message": "测试消息已发送，请查看收件箱 / 企业微信"})


@routes.post("/api/login/start")
async def api_login_start(request: web.Request) -> web.Response:
    body = await request.json()
    session = await login.start("wechat" if body.get("method") == "wechat" else "qq")
    return web.json_response(session.snapshot())


@routes.get("/api/login/status")
async def api_login_status(_: web.Request) -> web.Response:
    session = login.current()
    if not session:
        return web.json_response({"state": "idle", "message": ""})
    return web.json_response(session.snapshot())


@routes.get("/api/qr")
async def api_qr(_: web.Request) -> web.StreamResponse:
    session = login.current()
    if not session or not session.qr_path or not Path(session.qr_path).exists():
        return web.json_response({"ok": False, "message": "二维码不存在"}, status=404)
    response = web.FileResponse(session.qr_path)
    response.headers["Cache-Control"] = "no-store"
    return response


@routes.get("/api/public-ip")
async def api_public_ip(_: web.Request) -> web.Response:
    ip = await wecom_channel.detect_public_ip()
    if not ip:
        return web.json_response({"ok": False, "message": "检测失败，请检查网络后重试"}, status=502)
    return web.json_response({"ok": True, "ip": ip, "masked": wecom_channel.mask_ip(ip)})


@routes.post("/api/push-now")
async def api_push_now(_: web.Request) -> web.Response:
    try:
        result = await pipeline.run_push(trigger="manual")
    except pipeline.LoginRequired as exc:
        return web.json_response({"ok": False, "message": str(exc), "code": "login"}, status=400)
    except ChannelError as exc:
        payload: dict[str, Any] = {"ok": False, "message": exc.message, "code": exc.code}
        if exc.code == 60020:
            payload["can_fallback"] = config.email_configured(config.load_settings())
        return web.json_response(payload, status=400)
    except Exception as exc:
        logger.exception("手动推送失败")
        return web.json_response({"ok": False, "message": str(exc)}, status=500)

    return web.json_response(
        {
            "ok": True,
            "message": result.message,
            "channel": result.channel,
            "fallback_reason": result.fallback_reason,
            "summary": result.summary,
            "image": result.image.name if result.image else "",
        }
    )


@routes.get("/api/latest-image")
async def api_latest_image(_: web.Request) -> web.StreamResponse:
    images = sorted(paths.OUT_DIR.glob("shop_*.jpg"))
    if not images:
        return web.json_response({"ok": False, "message": "还没有生成图片"}, status=404)
    response = web.FileResponse(images[-1])
    response.headers["Cache-Control"] = "no-store"
    return response


@routes.get("/api/autostart")
async def api_autostart_get(_: web.Request) -> web.Response:
    return web.json_response({"enabled": _autostart_state()})


@routes.post("/api/autostart")
async def api_autostart_set(request: web.Request) -> web.Response:
    from . import autostart

    body = await request.json()
    enabled = bool(body.get("enabled"))
    try:
        autostart.set_enabled(enabled)
    except RuntimeError as exc:
        return web.json_response({"ok": False, "message": str(exc)}, status=400)
    return web.json_response({"ok": True, "enabled": enabled})


@routes.post("/api/open-data-dir")
async def api_open_data_dir(_: web.Request) -> web.Response:
    try:
        os.startfile(paths.DATA_DIR)  # type: ignore[attr-defined]
    except Exception as exc:
        return web.json_response({"ok": False, "message": str(exc)}, status=500)
    return web.json_response({"ok": True})


@routes.get("/favicon.ico")
async def favicon(_: web.Request) -> web.Response:
    return web.Response(status=204)


async def start_server(preferred_port: int | None = None) -> tuple[web.AppRunner, str]:
    """启动本地服务，端口被占用时自动顺延。返回 (runner, url)。"""
    app = web.Application(middlewares=[local_only])
    app.add_routes(routes)

    runner = web.AppRunner(app)
    await runner.setup()

    candidates = [preferred_port] if preferred_port else list(PORT_CANDIDATES)
    last_error: Exception | None = None
    for port in candidates:
        if not port:
            continue
        site = web.TCPSite(runner, "127.0.0.1", port)
        try:
            await site.start()
        except OSError as exc:
            last_error = exc
            continue
        logger.info("设置面板已启动：http://127.0.0.1:%s", port)
        return runner, f"http://127.0.0.1:{port}"

    await runner.cleanup()
    raise RuntimeError(f"无法启动本地设置面板：{last_error}")

INDEX_HTML = r'''<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="csrf-token" content="__CSRF_TOKEN__">
<title>无畏契约商店推送 · 设置</title>
<style>
  :root{
    --bg:#0f1923; --panel:#16222e; --panel-2:#1c2b3a; --line:#26384a;
    --text:#eef2f6; --sub:#8a9bb0; --accent:#ff4655; --accent-2:#ffe278;
    --ok:#3ecf8e; --warn:#ffb84d; --err:#ff6b6b;
  }
  *{box-sizing:border-box}
  body{
    margin:0; background:radial-gradient(1200px 600px at 20% -10%, #1d2f42 0%, var(--bg) 55%) fixed;
    color:var(--text); font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","Microsoft YaHei",sans-serif;
    font-size:14px; line-height:1.6; padding:28px 20px 96px;
  }
  .wrap{max-width:1000px;margin:0 auto}
  header{display:flex;align-items:center;justify-content:space-between;gap:16px;margin-bottom:22px;flex-wrap:wrap}
  .brand{display:flex;align-items:center;gap:14px}
  .logo{
    width:46px;height:46px;border-radius:12px;background:linear-gradient(135deg,var(--accent),#b32b3a);
    display:flex;align-items:center;justify-content:center;font-weight:800;font-size:22px;
    box-shadow:0 8px 24px rgba(255,70,85,.3)
  }
  h1{font-size:20px;margin:0;letter-spacing:.5px}
  .sub{margin:2px 0 0;color:var(--sub);font-size:12px}
  .head-right{display:flex;align-items:center;gap:12px}
  .pill{font-size:12px;padding:6px 12px;border-radius:999px;border:1px solid var(--line);background:var(--panel)}
  .pill.ok{color:var(--ok);border-color:rgba(62,207,142,.35)}
  .pill.warn{color:var(--warn);border-color:rgba(255,184,77,.35)}
  .pill.err{color:var(--err);border-color:rgba(255,107,107,.35)}
  .link{background:none;border:none;color:var(--sub);cursor:pointer;font-size:12px;text-decoration:underline;padding:0}
  .link:hover{color:var(--text)}
  .banner{
    border:1px solid rgba(255,184,77,.35);background:rgba(255,184,77,.08);color:#ffd590;
    padding:12px 16px;border-radius:12px;margin-bottom:18px;font-size:13px;white-space:pre-wrap
  }
  .grid{display:grid;grid-template-columns:1fr 1fr;gap:18px}
  @media (max-width:860px){.grid{grid-template-columns:1fr}}
  .card{
    background:linear-gradient(180deg,var(--panel) 0%,#141f2a 100%);
    border:1px solid var(--line);border-radius:16px;padding:20px 20px 22px;
  }
  .card h2{font-size:15px;margin:0 0 4px;display:flex;align-items:center;gap:8px}
  .tag{font-size:10px;color:var(--accent);border:1px solid rgba(255,70,85,.4);border-radius:6px;padding:2px 6px}
  .hint{color:var(--sub);font-size:12px;margin:6px 0 14px}
  .hint.warn{color:#ffc46b}
  .ip-row{
    display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin:4px 0 6px;padding:10px 12px;
    border-radius:10px;background:#0e1822;border:1px solid var(--line)
  }
  .ip-row .ip-label{color:var(--sub);font-size:12px}
  .ip-row code{font-family:ui-monospace,Consolas,monospace;font-size:13px;letter-spacing:.5px}
  .ip-row button{padding:5px 12px;font-size:12px}
  .seg{display:flex;background:#0e1822;border:1px solid var(--line);border-radius:10px;padding:3px;margin-bottom:14px}
  .seg button{
    flex:1;background:none;border:none;color:var(--sub);padding:8px 6px;border-radius:8px;
    cursor:pointer;font-size:13px;transition:.15s
  }
  .seg button.active{background:var(--panel-2);color:var(--text);box-shadow:0 2px 8px rgba(0,0,0,.25)}
  label.field{display:block;margin-bottom:12px}
  label.field span{display:block;color:var(--sub);font-size:12px;margin-bottom:5px}
  input,select{
    width:100%;padding:9px 11px;border-radius:9px;border:1px solid var(--line);
    background:#0e1822;color:var(--text);font-size:13px;font-family:inherit;outline:none
  }
  input:focus,select:focus{border-color:rgba(255,70,85,.6)}
  .two{display:grid;grid-template-columns:1fr 1fr;gap:12px}
  .three{display:grid;grid-template-columns:2fr 1fr 1.2fr;gap:12px}
  .row{display:flex;align-items:center;gap:12px;margin-top:4px;flex-wrap:wrap}
  button.action{
    background:var(--panel-2);color:var(--text);border:1px solid var(--line);border-radius:10px;
    padding:9px 16px;cursor:pointer;font-size:13px;font-family:inherit;transition:.15s
  }
  button.action:hover{border-color:#3c5166}
  button.primary{background:linear-gradient(135deg,var(--accent),#d93a48);border:none;color:#fff;font-weight:600}
  button.primary:hover{filter:brightness(1.07)}
  button:disabled{opacity:.55;cursor:not-allowed}
  .inline-msg{font-size:12px;color:var(--sub)}
  .inline-msg.ok{color:var(--ok)}
  .inline-msg.err{color:var(--err)}
  .qrbox{display:flex;flex-direction:column;align-items:center;gap:8px;margin-top:16px}
  .qrbox img{width:190px;height:190px;background:#fff;border-radius:12px;padding:8px}
  .qrbox p{margin:0;color:var(--sub);font-size:12px;text-align:center}
  .account{margin:12px 0 0;color:var(--sub);font-size:12px}
  .acc-list{display:flex;flex-direction:column;gap:10px;margin-top:6px}
  .acc-item{
    display:flex;align-items:center;gap:10px;padding:10px 12px;border-radius:12px;
    background:#0e1822;border:1px solid var(--line)
  }
  .acc-main{flex:1;min-width:0}
  .acc-name{display:flex;align-items:center;gap:8px;font-size:13px;font-weight:600}
  .acc-meta{color:var(--sub);font-size:11px;margin-top:2px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .acc-item button{
    background:var(--panel-2);border:1px solid var(--line);color:var(--text);border-radius:8px;
    padding:5px 10px;font-size:12px;font-family:inherit;cursor:pointer;flex:none
  }
  .acc-item button:hover{border-color:#3c5166}
  .acc-item button.danger{color:var(--err);border-color:rgba(255,107,107,.35)}
  .tag-expired{color:var(--err);border:1px solid rgba(255,107,107,.4);border-radius:6px;padding:1px 6px;font-size:10px;font-weight:400}
  .acc-empty{
    color:var(--sub);font-size:12px;text-align:center;padding:16px;border:1px dashed var(--line);border-radius:12px
  }
  .modal{
    position:fixed;inset:0;background:rgba(6,12,18,.72);display:flex;align-items:center;
    justify-content:center;padding:20px;z-index:30
  }
  .modal-box{
    width:100%;max-width:380px;background:var(--panel);border:1px solid var(--line);
    border-radius:16px;padding:22px;box-shadow:0 20px 50px rgba(0,0,0,.5)
  }
  .modal-box h3{margin:0 0 6px;font-size:16px}
  .modal-box input{margin-top:10px}
  .switch{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:10px 0;border-top:1px solid rgba(38,56,74,.6)}
  .switch:first-of-type{border-top:none}
  .switch .label{font-size:13px}
  .switch .desc{color:var(--sub);font-size:11px;margin-top:2px}
  .switch input{width:auto}
  .checks{display:flex;gap:18px;margin:8px 0 6px}
  .checks label{display:flex;align-items:center;gap:7px;color:var(--text);font-size:13px;cursor:pointer}
  .checks input{width:auto}
  .last{
    margin-top:16px;padding:12px 14px;border-radius:10px;background:#0e1822;border:1px solid var(--line);
    color:var(--sub);font-size:12px;white-space:pre-wrap;max-height:220px;overflow:auto
  }
  .last b{color:var(--text)}
  .preview{margin-top:14px}
  .preview img{width:100%;border-radius:12px;border:1px solid var(--line);display:block}
  .sticky-bar{
    position:fixed;left:0;right:0;bottom:0;background:rgba(15,25,35,.92);backdrop-filter:blur(10px);
    border-top:1px solid var(--line);padding:12px 20px;display:flex;align-items:center;justify-content:center;gap:14px
  }
  .sticky-bar button{padding:11px 30px;font-size:14px}
  .toast{
    position:fixed;right:22px;bottom:82px;max-width:340px;background:#22303f;border:1px solid var(--line);
    color:var(--text);padding:12px 16px;border-radius:12px;font-size:13px;box-shadow:0 12px 30px rgba(0,0,0,.4);
    opacity:0;transform:translateY(8px);transition:.2s;pointer-events:none;z-index:20;white-space:pre-wrap
  }
  .toast.show{opacity:1;transform:translateY(0)}
  .toast.err{border-color:rgba(255,107,107,.5)}
  .toast.ok{border-color:rgba(62,207,142,.5)}
  .hidden{display:none !important}
  .stats{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin-bottom:18px}
  .stat{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:10px 14px}
  .stat .k{color:var(--sub);font-size:11px}
  .stat .v{font-size:13px;margin-top:2px}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <div class="brand">
      <div class="logo">V</div>
      <div>
        <h1>无畏契约商店推送</h1>
        <p class="sub">个人版 · 纯本地运行 · 无需公网 IP / 域名 / 服务器</p>
      </div>
    </div>
    <div class="head-right">
      <span id="statusPill" class="pill">加载中…</span>
      <button class="link" id="openDirBtn">数据目录</button>
    </div>
  </header>

  <div id="banner" class="banner hidden"></div>

  <div class="stats">
    <div class="stat"><div class="k">账号</div><div class="v" id="statLogin">-</div></div>
    <div class="stat"><div class="k">下次定时推送</div><div class="v" id="statNext">-</div></div>
    <div class="stat"><div class="k">上次推送</div><div class="v" id="statLast">-</div></div>
  </div>

  <div class="grid">
    <section class="card">
      <h2>1 · 登录游戏账号 <span class="tag">国服</span></h2>
      <p class="hint">用手机 QQ 或微信扫码。凭证只保存在本机 data 目录，不会上传到任何服务器。</p>
      <div class="seg" id="loginTabs">
        <button data-method="qq" class="active">QQ 扫码</button>
        <button data-method="wechat">微信扫码</button>
      </div>
      <button class="action primary" id="loginBtn">生成登录二维码</button>
      <div class="qrbox hidden" id="qrBox">
        <img id="qrImg" alt="登录二维码">
        <p id="qrMsg"></p>
      </div>
      <p class="account" id="accountLine"></p>
    </section>

    <section class="card">
      <h2>2 · 已绑定账号 <span class="tag" id="accCount">0/10</span></h2>
      <p class="hint">最多可绑定 10 个账号。推送时会合并成一条消息，并按账号名分区，方便区分。</p>
      <div class="acc-list" id="accList"><div class="acc-empty">还没有绑定账号</div></div>
      <div class="row" style="margin-top:12px">
        <button class="action" id="exportBtn" disabled>导出凭证包</button>
      </div>
      <p class="hint">部署服务器版后，用「导出凭证包」把账号迁移过去；导入完成后请删除导出文件。</p>
    </section>

    <section class="card">
      <h2>3 · 选择推送渠道</h2>
      <div class="seg" id="channelTabs">
        <button data-channel="email" class="active">邮件（推荐）</button>
        <button data-channel="wecom">企业微信</button>
      </div>

      <div id="emailPane">
        <div class="two">
          <label class="field"><span>发件邮箱</span><input id="e_user" placeholder="you@qq.com"></label>
          <label class="field"><span>授权码 / 密码</span><input id="e_pass" type="password" placeholder="邮箱授权码"></label>
        </div>
        <label class="field"><span>收件邮箱（留空则发给发件邮箱）</span><input id="e_to" placeholder="默认同上"></label>
        <div class="three">
          <label class="field"><span>SMTP 服务器</span><input id="e_host" placeholder="smtp.qq.com"></label>
          <label class="field"><span>端口</span><input id="e_port" type="number" placeholder="465"></label>
          <label class="field"><span>加密方式</span>
            <select id="e_security">
              <option value="ssl">SSL（465）</option>
              <option value="starttls">STARTTLS（587）</option>
            </select>
          </label>
        </div>
        <label class="field"><span>发件人显示名</span><input id="e_name" placeholder="无畏契约商店"></label>
        <p class="hint">QQ / 163 邮箱需要在网页版设置里开启 SMTP 并生成「授权码」，不是登录密码。</p>
        <p class="hint">出于安全考虑，程序不提供「不加密 SMTP」；服务器不支持 SSL / STARTTLS 时不会发送，避免授权码明文上网。</p>
      </div>

      <div id="wecomPane" class="hidden">
        <div class="two">
          <label class="field"><span>企业 ID（corpid）</span><input id="w_corpid" placeholder="ww..."></label>
          <label class="field"><span>应用 AgentId</span><input id="w_agentid" placeholder="1000002"></label>
        </div>
        <label class="field"><span>应用 Secret</span><input id="w_secret" type="password" placeholder="应用 Secret"></label>
        <label class="field"><span>接收人（userid，多个用 | 分隔）</span><input id="w_touser" placeholder="@all"></label>
        <div class="ip-row">
          <span class="ip-label">本机出口 IP</span>
          <code id="publicIp">未检测</code>
          <button class="action" id="ipCopy" disabled>复制</button>
          <button class="action" id="ipRefresh">检测 IP</button>
          <span class="inline-msg" id="ipMsg"></span>
        </div>
        <p class="hint">点「复制」得到完整 IP，粘贴到企业微信后台「应用管理 → 自建应用 → 企业可信 IP」。页面上只做打码显示，完整地址只写入剪贴板。</p>
        <p class="hint warn">点击「检测 IP」时，程序会依次请求 myip.ipip.net、icanhazip.com、ifconfig.me、api.ipify.org 获取你的公网 IP；除此之外不会向任何第三方发送数据。如不希望使用，可自行在百度搜索「IP」查看后手填。</p>
        <p class="hint warn">自建应用要求把本机出口 IP 加入「企业可信 IP」。家用宽带 IP 变化时会返回 60020，程序会自动改用邮件（需同时配置邮件）。</p>
      </div>

      <div class="row">
        <button class="action" id="testBtn">发送测试消息</button>
        <span class="inline-msg" id="testMsg"></span>
      </div>
    </section>

    <section class="card">
      <h2>4 · 推送设置</h2>
      <label class="field" style="max-width:180px"><span>每天推送时间</span><input id="pushTime" type="time"></label>
      <div class="checks">
        <label><input type="checkbox" id="secDaily" checked>每日商店</label>
        <label><input type="checkbox" id="secKingdom" checked>王国商店</label>
      </div>
      <div class="switch">
        <div>
          <div class="label">每日定时推送</div>
          <div class="desc">错过时间会在下次开机后补推当天内容</div>
        </div>
        <input type="checkbox" id="scheduleOn">
      </div>
      <div class="switch">
        <div>
          <div class="label">开机自动启动</div>
          <div class="desc">写入当前用户的启动项，无需管理员权限</div>
        </div>
        <input type="checkbox" id="autoStart">
      </div>
      <p class="hint" style="margin-top:14px">图片为 2×2 商品卡片，同时附带文字版摘要；两个商店会拼在同一张图里。</p>
    </section>

    <section class="card">
      <h2>5 · 立即推送</h2>
      <p class="hint">先保存设置并测试渠道，成功后可以立刻推送一次确认效果。</p>
      <button class="action primary" id="pushBtn">现在推送一次</button>
      <div class="last" id="lastBox">暂无推送记录</div>
      <div class="preview hidden" id="previewBox"><img id="previewImg" alt="商店图片"></div>
    </section>
  </div>
</div>

<div class="modal hidden" id="nameModal">
  <div class="modal-box">
    <h3 id="nameModalTitle">给账号起个名字</h3>
    <p class="hint" id="nameModalHint">推送消息里会用这个名字区分账号，例如「大号」「小号」。</p>
    <input id="nameInput" maxlength="24" placeholder="账号1">
    <div class="row" style="justify-content:flex-end;margin-top:16px">
      <button class="action" id="nameCancel">取消</button>
      <button class="action primary" id="nameOk">保存</button>
    </div>
  </div>
</div>

<div class="modal hidden" id="exportModal">
  <div class="modal-box">
    <h3>导出凭证包</h3>
    <p class="hint">把已绑定账号的登录凭证打包成加密文件，用于在服务器版导入。邮箱授权码、企业微信 Secret 不在包内，需在服务器版重新填写。</p>
    <label class="field"><span>设置口令（至少 8 位）</span><input id="exportPass" type="password" placeholder="导入服务器版时需要同一个口令"></label>
    <label class="field"><span>再次输入口令</span><input id="exportPass2" type="password"></label>
    <p class="hint">凭证包含游戏登录凭证：导入完成后请删除该文件；请记住口令，忘记口令只能重新导出。</p>
    <div class="row" style="justify-content:flex-end;margin-top:16px">
      <button class="action" id="exportCancel">取消</button>
      <button class="action primary" id="exportOk">生成并下载</button>
    </div>
    <span class="inline-msg" id="exportMsg"></span>
  </div>
</div>

<div class="sticky-bar">
  <button class="action primary" id="saveBtn">保存全部设置</button>
  <span class="inline-msg" id="saveMsg"></span>
</div>
<div class="toast" id="toast"></div>

<script>
const state = { channel: 'email', loginMethod: 'qq', status: null, qrKey: '', poll: null, nameTarget: null, accountPromptKey: '', publicIp: '' };
const $ = (id) => document.getElementById(id);
const val = (id) => ($(id) ? $(id).value.trim() : '');

// 常见邮箱的 SMTP 预设：填完发件邮箱后自动补齐服务器 / 端口 / 加密方式
const SMTP_GUESS = {
  'qq.com': ['smtp.qq.com', 465, 'ssl'],
  'foxmail.com': ['smtp.qq.com', 465, 'ssl'],
  '163.com': ['smtp.163.com', 465, 'ssl'],
  '126.com': ['smtp.126.com', 465, 'ssl'],
  'yeah.net': ['smtp.yeah.net', 465, 'ssl'],
  'gmail.com': ['smtp.gmail.com', 587, 'starttls'],
  'googlemail.com': ['smtp.gmail.com', 587, 'starttls'],
  'outlook.com': ['smtp.office365.com', 587, 'starttls'],
  'hotmail.com': ['smtp.office365.com', 587, 'starttls'],
  'live.com': ['smtp.office365.com', 587, 'starttls'],
  'sina.com': ['smtp.sina.com', 465, 'ssl'],
  'aliyun.com': ['smtp.aliyun.com', 465, 'ssl'],
  '139.com': ['smtp.139.com', 465, 'ssl'],
  '189.cn': ['smtp.189.cn', 465, 'ssl']
};
let autoHost = '';

function smtpPreset(address) {
  const at = address.lastIndexOf('@');
  if (at < 0) return null;
  const domain = address.slice(at + 1).trim().toLowerCase();
  if (!domain || domain.indexOf('.') < 0) return null;
  return SMTP_GUESS[domain] || ['smtp.' + domain, 465, 'ssl'];
}

function applySmtpPreset() {
  const preset = smtpPreset(val('e_user'));
  if (!preset) return;
  const current = val('e_host');
  // 只覆盖空值或上一次自动填写的值，用户手填的不动
  if (current && current !== autoHost) return;
  $('e_host').value = preset[0];
  $('e_port').value = preset[1];
  $('e_security').value = preset[2];
  autoHost = preset[0];
}

function toast(message, kind) {
  const el = $('toast');
  el.textContent = message;
  el.className = 'toast show ' + (kind || '');
  clearTimeout(el._timer);
  el._timer = setTimeout(() => { el.className = 'toast ' + (kind || ''); }, 4200);
}

// 本地接口只接受本站页面发起的请求：带上自定义头 + 每次启动随机生成的令牌
const CSRF_TOKEN = (document.querySelector('meta[name="csrf-token"]') || {}).content || '';

async function api(path, options) {
  const opts = Object.assign({}, options || {});
  opts.headers = Object.assign({
    'Content-Type': 'application/json',
    'X-Requested-With': 'ValorantPushPersonal',
    'X-CSRF-Token': CSRF_TOKEN
  }, (options && options.headers) || {});
  const res = await fetch(path, opts);
  let data = {};
  try { data = await res.json(); } catch (e) {}
  if (!res.ok) {
    const err = new Error(data.message || ('请求失败（' + res.status + '）'));
    err.data = data;
    throw err;
  }
  return data;
}

function setMsg(id, text, kind) {
  const el = $(id);
  el.textContent = text || '';
  el.className = 'inline-msg ' + (kind || '');
}

function fillSettings(s) {
  state.channel = s.channel === 'wecom' ? 'wecom' : 'email';
  state.loginMethod = s.login_method === 'wechat' ? 'wechat' : 'qq';

  document.querySelectorAll('#channelTabs button').forEach((b) => {
    b.classList.toggle('active', b.dataset.channel === state.channel);
  });
  document.querySelectorAll('#loginTabs button').forEach((b) => {
    b.classList.toggle('active', b.dataset.method === state.loginMethod);
  });
  $('emailPane').classList.toggle('hidden', state.channel !== 'email');
  $('wecomPane').classList.toggle('hidden', state.channel !== 'wecom');

  $('e_user').value = s.email.username || '';
  $('e_pass').value = '';
  $('e_pass').placeholder = s.email.password_set ? '已保存，留空保持不变' : '邮箱授权码';
  $('e_to').value = s.email.to || '';
  $('e_host').value = s.email.smtp_host || '';
  $('e_port').value = s.email.smtp_port || 465;
  $('e_security').value = s.email.security || 'ssl';
  $('e_name').value = s.email.from_name || '';

  // 已保存的服务器如果正好是自动匹配的值，就继续跟随邮箱域名变化
  const preset = smtpPreset(s.email.username || '');
  autoHost = preset && preset[0] === (s.email.smtp_host || '') ? preset[0] : '';
  if (!s.email.smtp_host) applySmtpPreset();

  $('w_corpid').value = s.wecom.corpid || '';
  $('w_agentid').value = s.wecom.agentid || '';
  $('w_secret').value = '';
  $('w_secret').placeholder = s.wecom.secret_set ? '已保存，留空保持不变' : '应用 Secret';
  $('w_touser').value = s.wecom.touser || '@all';

  const hh = String(s.push_hour).padStart(2, '0');
  const mm = String(s.push_minute).padStart(2, '0');
  $('pushTime').value = hh + ':' + mm;
  $('secDaily').checked = (s.sections || []).includes('dailystore');
  $('secKingdom').checked = (s.sections || []).includes('kingdomstore');
  $('scheduleOn').checked = !!s.schedule_enabled;
}

function collectDraft() {
  const time = ($('pushTime').value || '08:05').split(':');
  const sections = [];
  if ($('secDaily').checked) sections.push('dailystore');
  if ($('secKingdom').checked) sections.push('kingdomstore');
  return {
    channel: state.channel,
    login_method: state.loginMethod,
    schedule_enabled: $('scheduleOn').checked,
    push_hour: Number(time[0] || 8),
    push_minute: Number(time[1] || 5),
    sections: sections.length ? sections : ['dailystore'],
    email: {
      smtp_host: val('e_host'),
      smtp_port: Number(val('e_port') || 465),
      security: val('e_security'),
      username: val('e_user'),
      password: val('e_pass'),
      to: val('e_to'),
      from_name: val('e_name')
    },
    wecom: {
      corpid: val('w_corpid'),
      agentid: val('w_agentid'),
      secret: val('w_secret'),
      touser: val('w_touser')
    }
  };
}

function renderStatus(s) {
  state.status = s;
  const pill = $('statusPill');
  if (s.auth_expired) {
    pill.textContent = '登录已失效';
    pill.className = 'pill err';
  } else if (s.logged_in) {
    pill.textContent = '已登录';
    pill.className = 'pill ok';
  } else {
    pill.textContent = '未登录';
    pill.className = 'pill warn';
  }

  $('statNext').textContent = s.schedule_enabled ? (s.next_run || '明日') : '已关闭';
  $('statLast').textContent = s.last_push_at
    ? s.last_push_at + (s.last_push_ok ? ' 成功' : ' 失败')
    : '暂无记录';

  const count = s.account_count || 0;
  const limit = s.account_limit || 10;
  $('statLogin').textContent = count
    ? count + ' 个账号' + (s.expired_accounts && s.expired_accounts.length
        ? '（' + s.expired_accounts.length + ' 个失效）' : '')
    : '未登录';
  $('accCount').textContent = count + '/' + limit;
  renderAccounts(s.accounts || []);

  $('accountLine').textContent = count
    ? '已绑定 ' + count + ' 个账号，凭证只保存在本机 data 目录。'
    : '尚未登录。扫码后即可获取商店数据，最多可绑定 ' + limit + ' 个账号。';

  $('autoStart').checked = !!s.autostart;

  const banner = $('banner');
  const lines = [];
  if (s.auth_expired) {
    lines.push('登录已失效：每日推送会暂停，请重新扫码登录。');
  } else if (s.expired_accounts && s.expired_accounts.length) {
    lines.push('以下账号登录已失效，推送时会跳过：' + s.expired_accounts.join('、'));
  }
  if (s.last_error) {
    lines.push('最近一次错误：' + s.last_error);
  }
  banner.textContent = lines.join('\n');
  banner.classList.toggle('hidden', lines.length === 0);

  const box = $('lastBox');
  if (s.last_summary) {
    box.textContent = '上次推送：' + (s.last_push_at || '-') +
      ' · ' + (s.last_channel === 'wecom' ? '企业微信' : '邮件') + '\n\n' + s.last_summary;
  } else if (s.last_error) {
    box.textContent = s.last_error;
  }
}

function renderAccounts(list) {
  $('exportBtn').disabled = list.length === 0;
  const box = $('accList');
  box.innerHTML = '';
  if (!list || !list.length) {
    const empty = document.createElement('div');
    empty.className = 'acc-empty';
    empty.textContent = '还没有绑定账号';
    box.appendChild(empty);
    return;
  }

  list.forEach((acc) => {
    const item = document.createElement('div');
    item.className = 'acc-item';

    const main = document.createElement('div');
    main.className = 'acc-main';
    const nameRow = document.createElement('div');
    nameRow.className = 'acc-name';
    nameRow.textContent = acc.name || '账号';
    if (acc.auth_expired) {
      const tag = document.createElement('span');
      tag.className = 'tag-expired';
      tag.textContent = '登录已失效';
      nameRow.appendChild(tag);
    }
    const meta = document.createElement('div');
    meta.className = 'acc-meta';
    meta.textContent = (acc.login_type === 'wechat' ? '微信' : 'QQ') + ' · ' + acc.user_id_masked +
      (acc.added_at ? ' · 绑定于 ' + acc.added_at : '');
    main.appendChild(nameRow);
    main.appendChild(meta);

    const renameBtn = document.createElement('button');
    renameBtn.textContent = '改名';
    renameBtn.addEventListener('click', () => openNameModal(acc, false));

    const removeBtn = document.createElement('button');
    removeBtn.className = 'danger';
    removeBtn.textContent = '解绑';
    removeBtn.addEventListener('click', () => removeAccount(acc));

    item.appendChild(main);
    item.appendChild(renameBtn);
    item.appendChild(removeBtn);
    box.appendChild(item);
  });
}

function openNameModal(account, isNew) {
  state.nameTarget = account;
  if (isNew && account.id) sessionStorage.setItem('valpush_named_' + account.id, '1');
  $('nameModalTitle').textContent = isNew ? '给账号起个名字' : '修改账号名';
  $('nameModalHint').textContent = isNew
    ? '推送消息里会用这个名字区分账号，例如「大号」「小号」。'
    : '改名后，下次推送就会使用新名字。';
  $('nameInput').value = account.name || '';
  $('nameModal').classList.remove('hidden');
  $('nameInput').focus();
  $('nameInput').select();
}

function closeNameModal() {
  state.nameTarget = null;
  $('nameModal').classList.add('hidden');
}

async function commitName() {
  const target = state.nameTarget;
  if (!target) return;
  const name = val('nameInput');
  if (!name) {
    toast('账号名不能为空', 'err');
    return;
  }
  $('nameOk').disabled = true;
  try {
    const res = await api('/api/accounts/rename', {
      method: 'POST',
      body: JSON.stringify({ id: target.id, name: name })
    });
    closeNameModal();
    toast(res.message || '已保存', 'ok');
    await loadAll();
  } catch (err) {
    toast(err.message, 'err');
  } finally {
    $('nameOk').disabled = false;
  }
}

async function removeAccount(account) {
  if (!confirm('确定解绑「' + (account.name || '账号') + '」吗？\n解绑后需要重新扫码才能恢复。')) return;
  try {
    const res = await api('/api/accounts/remove', {
      method: 'POST',
      body: JSON.stringify({ id: account.id })
    });
    toast(res.message || '已解绑', 'ok');
    await loadAll();
  } catch (err) {
    toast(err.message, 'err');
  }
}

async function loadAll() {
  try {
    const [status, settings] = await Promise.all([api('/api/status'), api('/api/settings')]);
    renderStatus(status);
    fillSettings(settings);
  } catch (err) {
    toast('加载失败：' + err.message, 'err');
  }
}

function openExportModal() {
  $('exportPass').value = ''; $('exportPass2').value = '';
  setMsg('exportMsg', '', 'info');
  $('exportModal').classList.remove('hidden');
  $('exportPass').focus();
}
function closeExportModal() { $('exportModal').classList.add('hidden'); }
async function commitExport() {
  const pw = $('exportPass').value, pw2 = $('exportPass2').value;
  if (pw.length < 8) { setMsg('exportMsg', '口令至少 8 位', 'warn'); return; }
  if (pw !== pw2) { setMsg('exportMsg', '两次输入的口令不一致', 'warn'); return; }
  $('exportOk').disabled = true;
  try {
    const data = await api('/api/credentials/export', { method: 'POST', body: JSON.stringify({ password: pw }) });
    const bin = atob(data.content_b64);
    const bytes = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
    const url = URL.createObjectURL(new Blob([bytes], { type: 'application/octet-stream' }));
    const a = document.createElement('a');
    a.href = url; a.download = data.filename;
    document.body.appendChild(a); a.click(); a.remove();
    URL.revokeObjectURL(url);
    closeExportModal();
    toast('已生成 ' + data.filename + '。这些账号导入服务器版后，请在个人版停止其定时推送；导入完成后请删除该文件。', 'ok');
  } catch (e) { setMsg('exportMsg', e.message, 'err'); }
  $('exportOk').disabled = false;
}

async function saveAll() {
  const btn = $('saveBtn');
  btn.disabled = true;
  try {
    const res = await api('/api/settings', { method: 'POST', body: JSON.stringify(collectDraft()) });
    await loadAll();
    setMsg('saveMsg', '已保存', 'ok');
    if (res.problems && res.problems.length) {
      toast('已保存，但还差：\n' + res.problems.join('\n'), 'err');
    } else {
      toast('设置已保存', 'ok');
    }
  } catch (err) {
    toast('保存失败：' + err.message, 'err');
  } finally {
    btn.disabled = false;
  }
}

function switchChannel(channel) {
  state.channel = channel;
  document.querySelectorAll('#channelTabs button').forEach((b) => {
    b.classList.toggle('active', b.dataset.channel === channel);
  });
  $('emailPane').classList.toggle('hidden', channel !== 'email');
  $('wecomPane').classList.toggle('hidden', channel !== 'wecom');
}

// 检测本机出口 IP：仅由「检测 IP」按钮显式触发，页面只显示打码结果
async function detectPublicIp() {
  $('publicIp').textContent = '检测中…';
  $('ipCopy').disabled = true;
  setMsg('ipMsg', '');
  try {
    const res = await api('/api/public-ip');
    state.publicIp = res.ip;
    $('publicIp').textContent = res.masked;
    $('ipCopy').disabled = false;
  } catch (err) {
    state.publicIp = '';
    $('publicIp').textContent = '检测失败';
    setMsg('ipMsg', err.message, 'err');
  }
}

async function copyPublicIp() {
  if (!state.publicIp) return;
  try {
    await navigator.clipboard.writeText(state.publicIp);
  } catch (err) {
    const area = document.createElement('textarea');
    area.value = state.publicIp;
    area.style.position = 'fixed';
    area.style.opacity = '0';
    document.body.appendChild(area);
    area.select();
    document.execCommand('copy');
    document.body.removeChild(area);
  }
  setMsg('ipMsg', '已复制完整 IP', 'ok');
}

async function testChannel() {
  const btn = $('testBtn');
  btn.disabled = true;
  setMsg('testMsg', '正在发送…');
  try {
    const res = await api('/api/test-channel', {
      method: 'POST',
      body: JSON.stringify({ channel: state.channel, draft: collectDraft() })
    });
    setMsg('testMsg', res.message, 'ok');
  } catch (err) {
    const data = err.data || {};
    setMsg('testMsg', err.message, 'err');
    if (data.code === 60020) {
      switchChannel('email');
      toast('企业微信返回 60020（可信 IP 不匹配），已切换到邮件渠道。\n如仍想用企业微信，请点「检测 IP」后更新后台白名单，并填写邮件配置后保存。', 'err');
      return;
    }
    toast(err.message, 'err');
  } finally {
    btn.disabled = false;
  }
}

async function pushNow() {
  const btn = $('pushBtn');
  btn.disabled = true;
  btn.textContent = '正在推送…';
  try {
    const res = await api('/api/push-now', { method: 'POST', body: '{}' });
    toast(res.fallback_reason || '推送成功', res.fallback_reason ? 'err' : 'ok');
    if (res.image) {
      $('previewBox').classList.remove('hidden');
      $('previewImg').src = '/api/latest-image?t=' + Date.now();
    }
    await loadAll();
    if (res.summary) $('lastBox').textContent = res.summary;
  } catch (err) {
    toast(err.message, 'err');
  } finally {
    btn.disabled = false;
    btn.textContent = '现在推送一次';
  }
}

function stopPolling() {
  if (state.poll) {
    clearInterval(state.poll);
    state.poll = null;
  }
}

function applyLogin(snapshot) {
  if (!snapshot) return;
  if (snapshot.state === 'starting' || snapshot.state === 'waiting') {
    $('qrBox').classList.remove('hidden');
    $('qrMsg').textContent = snapshot.message || '请扫码';
    if (snapshot.has_qr) {
      const key = snapshot.method + ':' + snapshot.message;
      if (key !== state.qrKey) {
        state.qrKey = key;
        $('qrImg').src = '/api/qr?t=' + Date.now();
      }
    }
  } else if (snapshot.state === 'success') {
    stopPolling();
    $('qrMsg').textContent = '登录成功';
    $('loginBtn').disabled = false;
    const account = snapshot.account;
    const key = account ? account.id + ':' + (account.updated ? '1' : '0') : '';
    toast(snapshot.message || '登录成功', 'ok');
    setTimeout(() => $('qrBox').classList.add('hidden'), 1500);
    if (account && key !== state.accountPromptKey) {
      state.accountPromptKey = key;
      const skip = account.updated || sessionStorage.getItem('valpush_named_' + account.id);
      loadAll().then(() => { if (!skip) openNameModal(account, true); });
    } else {
      loadAll();
    }
  } else if (snapshot.state === 'failed' || snapshot.state === 'timeout') {
    stopPolling();
    $('qrMsg').textContent = snapshot.message || '登录失败';
    $('loginBtn').disabled = false;
    toast(snapshot.message || '登录失败', 'err');
  }
}

async function startLogin() {
  $('loginBtn').disabled = true;
  $('qrBox').classList.remove('hidden');
  state.qrKey = '';
  $('qrImg').removeAttribute('src');
  $('qrMsg').textContent = '正在生成二维码…';
  try {
    const snapshot = await api('/api/login/start', {
      method: 'POST',
      body: JSON.stringify({ method: state.loginMethod })
    });
    applyLogin(snapshot);
    stopPolling();
    state.poll = setInterval(async () => {
      try {
        applyLogin(await api('/api/login/status'));
      } catch (e) {
        stopPolling();
      }
    }, 1500);
  } catch (err) {
    toast('生成二维码失败：' + err.message, 'err');
    $('loginBtn').disabled = false;
  }
}

$('loginTabs').addEventListener('click', (e) => {
  const btn = e.target.closest('button');
  if (!btn) return;
  state.loginMethod = btn.dataset.method;
  document.querySelectorAll('#loginTabs button').forEach((b) => b.classList.toggle('active', b === btn));
});

$('channelTabs').addEventListener('click', (e) => {
  const btn = e.target.closest('button');
  if (!btn) return;
  switchChannel(btn.dataset.channel);
});

$('e_user').addEventListener('input', applySmtpPreset);
$('e_user').addEventListener('change', applySmtpPreset);

$('loginBtn').addEventListener('click', startLogin);

$('nameOk').addEventListener('click', commitName);
$('nameCancel').addEventListener('click', closeNameModal);
$('nameInput').addEventListener('keydown', (e) => {
  if (e.key === 'Enter') {
    e.preventDefault();
    commitName();
  } else if (e.key === 'Escape') {
    closeNameModal();
  }
});
$('nameModal').addEventListener('click', (e) => {
  if (e.target === $('nameModal')) closeNameModal();
});
$('exportBtn').addEventListener('click', openExportModal);
$('exportCancel').addEventListener('click', closeExportModal);
$('exportModal').addEventListener('click', (e) => { if (e.target === $('exportModal')) closeExportModal(); });
$('exportOk').addEventListener('click', commitExport);
$('testBtn').addEventListener('click', testChannel);
$('ipRefresh').addEventListener('click', () => detectPublicIp());
$('ipCopy').addEventListener('click', copyPublicIp);
$('saveBtn').addEventListener('click', saveAll);
$('pushBtn').addEventListener('click', pushNow);

$('autoStart').addEventListener('change', async (e) => {
  try {
    await api('/api/autostart', { method: 'POST', body: JSON.stringify({ enabled: e.target.checked }) });
    toast(e.target.checked ? '已开启开机自启' : '已关闭开机自启', 'ok');
  } catch (err) {
    e.target.checked = !e.target.checked;
    toast(err.message, 'err');
  }
});

$('openDirBtn').addEventListener('click', async () => {
  try { await api('/api/open-data-dir', { method: 'POST', body: '{}' }); }
  catch (err) { toast(err.message, 'err'); }
});

loadAll().then(() => {
  api('/api/login/status').then((snapshot) => {
    if (snapshot && snapshot.state && snapshot.state !== 'idle') {
      applyLogin(snapshot);
      if (snapshot.state === 'waiting' || snapshot.state === 'starting') {
        state.poll = setInterval(async () => {
          try { applyLogin(await api('/api/login/status')); } catch (e) { stopPolling(); }
        }, 1500);
      }
    }
  }).catch(() => {});
});
</script>
</body>
</html>'''
