# 来源：拷贝自 app/web.py @ cd5e2d2（个人版 v1.1.0，含 M2 凭证包导出功能），随后按
# 《服务器版 M3 实施计划》Task 3 改造表裁剪——移除桌面专属项与扫码登录，
# 扫码卡片替换为凭证导入卡片（复用 credpack.import_accounts）。两版 UI 同源、独立演进。
"""服务器版管理界面（Web 向导：凭证导入 / 渠道与推送设置 / 账号管理）。"""
from __future__ import annotations

import asyncio
import base64
import logging
import os
import secrets
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from aiohttp import web

from . import __version__, accounts, auth, config, paths, pipeline, scheduler
from .channels import ChannelError
from .channels import email as email_channel
from .channels import wecom as wecom_channel
from .models import zone

logger = logging.getLogger(__name__)

CSRF_HEADER = "X-CSRF-Token"
REQUESTED_WITH = "ValorantPushPersonal"
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

# 免鉴权路径（设计规格表）：健康检查、登录/设置页与进入会话的前置接口、favicon
SAFE_PATHS = frozenset({"/healthz", "/login", "/setup", "/favicon.ico"})
SAFE_API_PATHS = frozenset({"/api/login", "/api/setup"})

_scheduler_task: asyncio.Task | None = None


def set_scheduler_task(task: asyncio.Task | None) -> None:
    """serve 模式注册调度任务，供 /healthz 汇报运行状态。"""
    global _scheduler_task
    _scheduler_task = task


def _cookie_secure(request: web.Request) -> bool:
    """Cookie Secure 自动探测：反代 HTTPS（X-Forwarded-Proto）或原生 https 时加 Secure；
    环境变量 VAL_SERVER_COOKIE_SECURE=1/0 强制覆盖（M4 文档写入安全清单）。"""
    override = os.getenv("VAL_SERVER_COOKIE_SECURE")
    if override is not None:
        return override == "1"
    if request.url.scheme == "https":
        return True
    return (request.headers.get("X-Forwarded-Proto") or "").strip().lower() == "https"


def _cross_site(request: web.Request) -> bool:
    """同源校验（个人版三重校验的服务器版改造）：Origin 若存在必须与 Host 同主机名；
    Sec-Fetch-Site 仅允许 same-origin/none；不再做 127.0.0.1 Host 白名单。"""
    origin = request.headers.get("Origin", "")
    if origin:
        try:
            parsed = urlsplit(origin)
        except ValueError:
            return True
        host = (request.host or "").split(":")[0].strip("[]")
        if parsed.scheme not in {"http", "https"} or parsed.hostname != host:
            return True
    fetch_site = request.headers.get("Sec-Fetch-Site", "")
    return bool(fetch_site) and fetch_site not in {"same-origin", "none"}


@web.middleware
async def auth_guard(request: web.Request, handler):
    """取代个人版 local_only：会话鉴权 + 会话级 CSRF + 同源校验（M3 设计规格表）。

    - 免鉴权：/healthz、/login、/setup、/favicon.ico；/api/login、/api/setup 只要求
      X-Requested-With 头防跨站表单
    - 未登录：页面 GET → 302 到 /setup（未初始化）或 /login；/api/* → 401 JSON
    - 已登录写接口：校验 X-CSRF-Token（会话级）+ X-Requested-With
    - 距签发超 24h 自动重签 cookie（滑动续期，token 不变）
    """
    path = request.path
    if path == "/healthz":
        return await handler(request)
    if _cross_site(request):
        return web.Response(status=403, text="cross-site request blocked")
    if path in SAFE_API_PATHS:
        if (
            request.method not in SAFE_METHODS
            and request.headers.get("X-Requested-With") != REQUESTED_WITH
        ):
            return web.json_response({"ok": False, "message": "非法请求"}, status=403)
        return await handler(request)
    if path in SAFE_PATHS:
        return await handler(request)
    token = request.cookies.get(auth.SESSION_COOKIE, "")
    session = auth.get_session(token)
    if session is None:
        if path.startswith("/api/"):
            return web.json_response({"ok": False, "code": "unauthorized"}, status=401)
        raise web.HTTPFound("/setup" if not auth.is_initialized() else "/login")
    if request.method not in SAFE_METHODS:
        csrf = request.headers.get(CSRF_HEADER, "")
        if (
            request.headers.get("X-Requested-With") != REQUESTED_WITH
            or not csrf
            or not secrets.compare_digest(csrf, session["csrf"])
        ):
            return web.json_response({"ok": False, "message": "无效的请求令牌"}, status=403)
    request["session"] = session
    response = await handler(request)
    if auth.should_reissue(session):
        session["issued_at"] = time.time()
        response.set_cookie(
            auth.SESSION_COOKIE,
            token,
            **auth.session_cookie_kwargs(_cookie_secure(request)),
        )
    return response


routes = web.RouteTableDef()


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
    }


@routes.get("/")
async def index(request: web.Request) -> web.Response:
    html = INDEX_HTML.replace("__CSRF_TOKEN__", request["session"]["csrf"])
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


@routes.get("/favicon.ico")
async def favicon(_: web.Request) -> web.Response:
    return web.Response(status=204)


def build_app() -> web.Application:
    """装配管理界面应用（中间件 + 路由）；启动由 __main__ 的 serve 模式负责。"""
    app = web.Application(middlewares=[auth_guard])
    app.add_routes(routes)
    return app


@routes.get("/healthz")
async def healthz(_: web.Request) -> web.Response:
    """健康检查（免鉴权）：仅版本号与调度状态。"""
    running = _scheduler_task is not None and not _scheduler_task.done()
    return web.json_response(
        {
            "ok": True,
            "version": __version__,
            "scheduler": "running" if running else "stopped",
        }
    )


@routes.get("/setup")
async def setup_page(_: web.Request) -> web.Response:
    if auth.is_initialized():
        raise web.HTTPFound("/login")
    return web.Response(text=SETUP_HTML, content_type="text/html")


@routes.get("/login")
async def login_page(_: web.Request) -> web.Response:
    if not auth.is_initialized():
        raise web.HTTPFound("/setup")
    return web.Response(text=LOGIN_HTML, content_type="text/html")


@routes.post("/api/setup")
async def api_setup(request: web.Request) -> web.Response:
    if auth.is_initialized():
        return web.json_response(
            {"ok": False, "message": "访问密码已设置，请直接登录"}, status=409
        )
    body = await request.json()
    try:
        auth.set_password(str(body.get("password") or ""), str(body.get("setup_code") or ""))
    except auth.AuthError as exc:
        return web.json_response({"ok": False, "message": str(exc)}, status=400)
    return _start_session(request)


@routes.post("/api/login")
async def api_login(request: web.Request) -> web.Response:
    if not auth.is_initialized():
        return web.json_response({"ok": False, "message": "请先完成初始化设置"}, status=409)
    body = await request.json()
    if auth.verify_password(str(body.get("password") or "")):
        auth.reset_failures()  # 成功登录即清零（含锁定，§0 决策 6）
        return _start_session(request)
    auth.register_failure()  # 未锁：计数+1（达标即锁）；已锁：剩余时长翻倍
    remaining = auth.lock_remaining()
    if remaining > 0:
        minutes = max(1, -(-int(remaining) // 60))
        return web.json_response(
            {"ok": False, "message": f"尝试次数过多，请 {minutes} 分钟后再试"}, status=429
        )
    await asyncio.sleep(auth.FAIL_DELAY)  # 固定延迟钝化时序探测
    return web.json_response({"ok": False, "message": "口令错误"}, status=401)


def _start_session(request: web.Request) -> web.Response:
    token, _ = auth.create_session()
    response = web.json_response({"ok": True, "redirect": "/"})
    response.set_cookie(
        auth.SESSION_COOKIE, token, **auth.session_cookie_kwargs(_cookie_secure(request))
    )
    return response


@routes.post("/api/logout")
async def api_logout(request: web.Request) -> web.Response:
    auth.drop_session(request.cookies.get(auth.SESSION_COOKIE, ""))
    response = web.json_response({"ok": True})
    response.del_cookie(auth.SESSION_COOKIE, path="/")
    return response

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
        <p class="sub">服务器版 · 部署于你的服务器</p>
      </div>
    </div>
    <div class="head-right">
      <span id="statusPill" class="pill">加载中…</span>
      <button class="link" id="logoutBtn">退出登录</button>
    </div>
  </header>

  <div id="banner" class="banner hidden"></div>

  <div class="stats">
    <div class="stat"><div class="k">账号</div><div class="v" id="statLogin">-</div></div>
    <div class="stat"><div class="k">下次定时推送</div><div class="v" id="statNext">-</div></div>
    <div class="stat"><div class="k">上次推送</div><div class="v" id="statLast">-</div></div>
  </div>

  <div class="grid">
    <!-- 卡片 1：凭证导入卡片由 Task 5 提供 -->

    <section class="card">
      <h2>2 · 已绑定账号 <span class="tag" id="accCount">0/10</span></h2>
      <p class="hint">最多可绑定 10 个账号。推送时会合并成一条消息，并按账号名分区，方便区分。</p>
      <div class="acc-list" id="accList"><div class="acc-empty">还没有绑定账号</div></div>
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
          <div class="desc">错过时间会在服务恢复后自动补推当天内容</div>
        </div>
        <input type="checkbox" id="scheduleOn">
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

<div class="sticky-bar">
  <button class="action primary" id="saveBtn">保存全部设置</button>
  <span class="inline-msg" id="saveMsg"></span>
</div>
<div class="toast" id="toast"></div>

<script>
const state = { channel: 'email', loginMethod: 'qq', status: null, nameTarget: null, publicIp: '' };
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
  if (res.status === 401) {
    location.href = '/login';
    const err = new Error('登录已失效，请重新登录');
    err.data = data;
    throw err;
  }
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

$('channelTabs').addEventListener('click', (e) => {
  const btn = e.target.closest('button');
  if (!btn) return;
  switchChannel(btn.dataset.channel);
});

$('e_user').addEventListener('input', applySmtpPreset);
$('e_user').addEventListener('change', applySmtpPreset);

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
$('testBtn').addEventListener('click', testChannel);
$('ipRefresh').addEventListener('click', () => detectPublicIp());
$('ipCopy').addEventListener('click', copyPublicIp);
$('saveBtn').addEventListener('click', saveAll);
$('pushBtn').addEventListener('click', pushNow);

$('logoutBtn').addEventListener('click', async () => {
  try { await api('/api/logout', { method: 'POST', body: '{}' }); } catch (e) {}
  location.href = '/login';
});

loadAll();
</script>
</body>
</html>'''

_AUTH_PAGE_CSS = """
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: system-ui, -apple-system, 'Segoe UI', 'Microsoft YaHei', sans-serif;
  background: #f5f6f8; min-height: 100vh; display: flex; align-items: center; justify-content: center; }
.box { background: #fff; border: 1px solid #e3e6ea; border-radius: 14px; padding: 32px 28px; width: 360px; }
h1 { font-size: 18px; margin-bottom: 4px; }
.sub { color: #8a919c; font-size: 13px; margin-bottom: 20px; }
label { display: block; font-size: 13px; color: #5a6472; margin: 12px 0 6px; }
input { width: 100%; padding: 10px 12px; border: 1px solid #d6dbe2; border-radius: 8px; font-size: 14px; }
button { width: 100%; margin-top: 18px; padding: 10px; border: 0; border-radius: 8px;
  background: #ff4655; color: #fff; font-size: 14px; cursor: pointer; }
button:disabled { opacity: .6; cursor: not-allowed; }
.msg { color: #c0392b; font-size: 13px; min-height: 18px; margin-top: 12px; }
.hint { color: #8a919c; font-size: 12px; margin-top: 10px; line-height: 1.7; }
"""

LOGIN_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>登录 · 无畏契约商店推送（服务器版）</title>
<style>
""" + _AUTH_PAGE_CSS + """</style>
</head>
<body>
<div class="box">
  <h1>无畏契约商店推送</h1>
  <p class="sub">服务器版管理界面</p>
  <label for="password">访问口令</label>
  <input id="password" type="password" autocomplete="current-password" autofocus>
  <button id="go">登录</button>
  <p class="msg" id="msg"></p>
</div>
<script>
const $ = (id) => document.getElementById(id);
async function submit() {
  $('msg').textContent = '';
  $('go').disabled = true;
  try {
    const res = await fetch('/api/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-Requested-With': 'ValorantPushPersonal' },
      body: JSON.stringify({ password: $('password').value })
    });
    const data = await res.json().catch(() => ({}));
    if (res.ok && data.ok) { location.href = '/'; return; }
    $('msg').textContent = data.message || '登录失败';
  } catch (e) {
    $('msg').textContent = '网络错误，请重试';
  }
  $('go').disabled = false;
}
$('go').addEventListener('click', submit);
$('password').addEventListener('keydown', (e) => { if (e.key === 'Enter') submit(); });
</script>
</body>
</html>
"""

SETUP_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>初始化 · 无畏契约商店推送（服务器版）</title>
<style>
""" + _AUTH_PAGE_CSS + """</style>
</head>
<body>
<div class="box">
  <h1>初始化管理界面</h1>
  <p class="sub">首次访问：设置访问口令</p>
  <label for="setupCode">一次性设置码（首次启动时打印在服务器日志）</label>
  <input id="setupCode" autocomplete="off" placeholder="8 位十六进制">
  <label for="password">访问口令（至少 8 位）</label>
  <input id="password" type="password" autocomplete="new-password">
  <label for="password2">再次输入口令</label>
  <input id="password2" type="password" autocomplete="new-password">
  <button id="go">完成设置</button>
  <p class="msg" id="msg"></p>
  <p class="hint">设置码只在首次启动时生成并打印到日志，设置成功后即失效；口令用于登录管理界面，请妥善保管。</p>
</div>
<script>
const $ = (id) => document.getElementById(id);
async function submit() {
  $('msg').textContent = '';
  const code = $('setupCode').value.trim();
  const pw = $('password').value, pw2 = $('password2').value;
  if (!code) { $('msg').textContent = '请输入设置码'; return; }
  if (pw.length < 8) { $('msg').textContent = '口令至少 8 位'; return; }
  if (pw !== pw2) { $('msg').textContent = '两次输入的口令不一致'; return; }
  $('go').disabled = true;
  try {
    const res = await fetch('/api/setup', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-Requested-With': 'ValorantPushPersonal' },
      body: JSON.stringify({ setup_code: code, password: pw })
    });
    const data = await res.json().catch(() => ({}));
    if (res.ok && data.ok) { location.href = '/'; return; }
    $('msg').textContent = data.message || '设置失败';
  } catch (e) {
    $('msg').textContent = '网络错误，请重试';
  }
  $('go').disabled = false;
}
$('go').addEventListener('click', submit);
document.addEventListener('keydown', (e) => { if (e.key === 'Enter') submit(); });
</script>
</body>
</html>
"""
