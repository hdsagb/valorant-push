"""本地设置向导（只监听 127.0.0.1，不需要公网 IP / 域名 / 服务器）。"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from aiohttp import web

from . import __version__, config, login, paths, pipeline, scheduler
from .channels import ChannelError
from .channels import email as email_channel
from .channels import wecom as wecom_channel
from .models import zone

logger = logging.getLogger(__name__)

HOSTS = ("127.0.0.1", "localhost", "::1")
PORT_CANDIDATES = range(8787, 8808)

routes = web.RouteTableDef()


@web.middleware
async def local_only(request: web.Request, handler):
    """只接受本机访问，避免被局域网页面的请求命中。"""
    host = (request.host or "").split(":")[0].strip("[]")
    if host not in HOSTS:
        return web.Response(status=403, text="local access only")
    return await handler(request)


def _masked_status() -> dict[str, Any]:
    settings = config.load_settings()
    state = config.load_state()
    credentials = config.credential_summary()
    next_push = scheduler.next_run(settings, state)
    now = datetime.now(zone())
    if next_push is None:
        next_run_text = ""
    elif not credentials["logged_in"]:
        next_run_text = "登录后开始"
    elif state["last_push_date"] != now.strftime("%Y-%m-%d") and next_push <= now:
        next_run_text = "即将补推"
    else:
        next_run_text = next_push.strftime("%m-%d %H:%M")
    return {
        "version": __version__,
        "logged_in": credentials["logged_in"],
        "login_type": credentials["login_type"],
        "user_id_masked": credentials["user_id_masked"],
        "auth_expired": bool(state["auth_expired"]),
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
    return web.Response(text=INDEX_HTML, content_type="text/html", charset="utf-8")


@routes.get("/api/status")
async def api_status(_: web.Request) -> web.Response:
    return web.json_response(_masked_status())


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


@routes.post("/api/push-now")
async def api_push_now(_: web.Request) -> web.Response:
    try:
        result = await pipeline.run_push(trigger="manual")
    except pipeline.LoginRequired as exc:
        return web.json_response({"ok": False, "message": str(exc), "code": "login"}, status=400)
    except ChannelError as exc:
        payload: dict[str, Any] = {"ok": False, "message": exc.message, "code": exc.code}
        if exc.code == 60020:
            payload["can_fallback"] = config.email_configured()
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
      <h2>2 · 选择推送渠道</h2>
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
              <option value="none">不加密（25）</option>
            </select>
          </label>
        </div>
        <label class="field"><span>发件人显示名</span><input id="e_name" placeholder="无畏契约商店"></label>
        <p class="hint">QQ / 163 邮箱需要在网页版设置里开启 SMTP 并生成「授权码」，不是登录密码。</p>
      </div>

      <div id="wecomPane" class="hidden">
        <div class="two">
          <label class="field"><span>企业 ID（corpid）</span><input id="w_corpid" placeholder="ww..."></label>
          <label class="field"><span>应用 AgentId</span><input id="w_agentid" placeholder="1000002"></label>
        </div>
        <label class="field"><span>应用 Secret</span><input id="w_secret" type="password" placeholder="应用 Secret"></label>
        <label class="field"><span>接收人（userid，多个用 | 分隔）</span><input id="w_touser" placeholder="@all"></label>
        <p class="hint warn">自建应用要求把本机出口 IP 加入「企业可信 IP」。家用宽带 IP 变化时会返回 60020，程序会自动改用邮件（需同时配置邮件）。</p>
      </div>

      <div class="row">
        <button class="action" id="testBtn">发送测试消息</button>
        <span class="inline-msg" id="testMsg"></span>
      </div>
    </section>

    <section class="card">
      <h2>3 · 推送设置</h2>
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
      <h2>4 · 立即推送</h2>
      <p class="hint">先保存设置并测试渠道，成功后可以立刻推送一次确认效果。</p>
      <button class="action primary" id="pushBtn">现在推送一次</button>
      <div class="last" id="lastBox">暂无推送记录</div>
      <div class="preview hidden" id="previewBox"><img id="previewImg" alt="商店图片"></div>
    </section>
  </div>
</div>

<div class="sticky-bar">
  <button class="action primary" id="saveBtn">保存全部设置</button>
  <span class="inline-msg" id="saveMsg"></span>
</div>
<div class="toast" id="toast"></div>

<script>
const state = { channel: 'email', loginMethod: 'qq', status: null, qrKey: '', poll: null };
const $ = (id) => document.getElementById(id);
const val = (id) => ($(id) ? $(id).value.trim() : '');

function toast(message, kind) {
  const el = $('toast');
  el.textContent = message;
  el.className = 'toast show ' + (kind || '');
  clearTimeout(el._timer);
  el._timer = setTimeout(() => { el.className = 'toast ' + (kind || ''); }, 4200);
}

async function api(path, options) {
  const opts = Object.assign({ headers: { 'Content-Type': 'application/json' } }, options || {});
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

  $('statLogin').textContent = s.logged_in
    ? (s.login_type === 'wechat' ? '微信' : 'QQ') + ' · ' + s.user_id_masked
    : '未登录';
  $('statNext').textContent = s.schedule_enabled ? (s.next_run || '明日') : '已关闭';
  $('statLast').textContent = s.last_push_at
    ? s.last_push_at + (s.last_push_ok ? ' 成功' : ' 失败')
    : '暂无记录';

  $('accountLine').textContent = s.logged_in
    ? '当前账号：' + s.user_id_masked + '（凭证保存在本机）'
    : '尚未登录。扫码后即可获取商店数据。';

  $('autoStart').checked = !!s.autostart;

  const banner = $('banner');
  const lines = [];
  if (s.auth_expired) {
    lines.push('登录已失效：每日推送会暂停，请重新扫码登录。');
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
      toast('企业微信返回 60020（可信 IP 不匹配），已切换到邮件渠道。\n请填写邮件配置后保存。', 'err');
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
    toast('登录成功', 'ok');
    setTimeout(() => $('qrBox').classList.add('hidden'), 1500);
    loadAll();
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

$('loginBtn').addEventListener('click', startLogin);
$('testBtn').addEventListener('click', testChannel);
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
