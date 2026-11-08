
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ============================================================
# [拷贝来源] app/vendor/val_shop_ref.py @ 个人版 commit 9e3f621（2026-11-08，v1.0.1）
# [禁改文件] 服务器版独立演进，禁止直接修改本文件任何逻辑；如需同步个人版改动，
#            重新整文件拷贝并更新来源 commit。
# [唯一允许差异] 全部 5 处 aiohttp.ClientSession 增加 trust_env=True
#            （R1-对策5：让 HTTPS_PROXY/HTTP_PROXY 环境变量对全部出站流量生效，
#            覆盖 QQ 登录、微信二维码、微信轮询、商店接口、图片下载）。
#            除此之外禁止任何修改。
# ============================================================
"""
无畏契约每日商店 -> 邮箱 单文件版

用法：
1. 先填写下面【邮箱配置】。
2. 第一次运行：没有 config.json 时，会让你选择 QQ / 微信扫码登录，成功后保存 userId/tid。
3. 以后运行：自动读取 config.json，获取每日商店，生成图片并发送邮件，然后退出。

OpenWrt 定时例子：
5 8 * * * /usr/bin/python3 /root/valorant_shop_email.py >> /root/valorant_shop.log 2>&1

依赖：
pip install aiohttp pillow
"""

import asyncio
import argparse
import base64
import hashlib
import html
import json
import logging
import os
import random
import re
import shutil
import smtplib
import string
import time
import urllib.parse
from datetime import datetime
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any, Dict, Optional, Tuple, List

import aiohttp


# =========================
# 邮箱配置：必须填写
# =========================
MAIL_USER = os.getenv("MAIL_USER")
MAIL_PASS = os.getenv("MAIL_PASS")
MAIL_TO = os.getenv("MAIL_TO") or MAIL_USER
MAIL_PROVIDER = os.getenv("MAIL_PROVIDER", "auto").strip().lower()
SMTP_USERNAME = os.getenv("SMTP_USERNAME", "").strip() or MAIL_USER

SMTP_PROVIDERS = {
    "163": ("smtp.163.com", 465, "ssl"),
    "126": ("smtp.126.com", 465, "ssl"),
    "qq": ("smtp.qq.com", 465, "ssl"),
    "gmail": ("smtp.gmail.com", 465, "ssl"),
    "yahoo": ("smtp.mail.yahoo.com", 465, "ssl"),
    "outlook": ("smtp-mail.outlook.com", 587, "starttls"),
}

# 邮件标题前缀
MAIL_SUBJECT_PREFIX = os.getenv("VAL_SHOP_SUBJECT_PREFIX", "无畏契约每日商店")

# 是否在配置缺失时也允许先登录账号
ALLOW_LOGIN_WITHOUT_MAIL_CONFIG = True
LOGIN_TIMEOUT_SECONDS = max(60, int(os.getenv("VAL_SHOP_LOGIN_TIMEOUT", "300")))


# =========================
# 文件配置
# =========================
BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.getenv("VAL_SHOP_DATA_DIR", str(BASE_DIR))).expanduser()
CONFIG_FILE = DATA_DIR / "config.json"
AUTH_EXPIRED_STATE_FILE = DATA_DIR / "auth-expired-notified"
TEMP_DIR = Path(os.getenv("VAL_SHOP_TEMP_DIR", str(BASE_DIR / "temp_val_shop"))).expanduser()
FONT_FILE = BASE_DIR / "fontFamily.ttf"

# 如果没有字体文件，就用系统默认字体
DEFAULT_FONT_SIZE = 36


logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("valorant_shop_email")


def _mask_email(address: str) -> str:
    """日志脱敏：a***@qq.com"""
    text = str(address or "").strip()
    if "@" not in text:
        return "***" if text else ""
    local, domain = text.split("@", 1)
    return f"{local[:1]}***@{domain}"


def _mask_user_id(user_id: Any) -> str:
    """日志脱敏：只保留末 4 位。"""
    text = str(user_id or "")
    if not text:
        return ""
    return "***" if len(text) <= 4 else "***" + text[-4:]


def infer_mail_provider(address: str) -> str:
    domain = (address.rsplit("@", 1)[-1] if "@" in address else "").lower()
    if domain == "163.com":
        return "163"
    if domain == "126.com":
        return "126"
    if domain in {"qq.com", "foxmail.com"}:
        return "qq"
    if domain in {"gmail.com", "googlemail.com"}:
        return "gmail"
    if domain in {"yahoo.com", "yahoo.com.cn"}:
        return "yahoo"
    if domain in {"outlook.com", "hotmail.com", "live.com", "msn.com"}:
        return "outlook"
    return "custom"


def parse_recipients(value: str) -> List[str]:
    return [item for item in re.split(r"[,;\s]+", value or "") if item]


def smtp_settings(address: str) -> Tuple[str, int, str]:
    provider = MAIL_PROVIDER
    if provider == "auto":
        provider = infer_mail_provider(address)

    defaults = SMTP_PROVIDERS.get(provider, ("", 465, "ssl"))
    host = os.getenv("SMTP_HOST", "").strip() or defaults[0]
    port = int(os.getenv("SMTP_PORT", "").strip() or defaults[1])
    security = os.getenv("SMTP_SECURITY", "").strip().lower() or defaults[2]
    if not host:
        raise RuntimeError("自定义 SMTP 必须填写 SMTP_HOST")
    if security not in {"ssl", "starttls", "plain"}:
        raise RuntimeError("SMTP_SECURITY 必须是 ssl、starttls 或 plain")
    return host, port, security


class ValorantShopEmail:
    # QQ 登录配置
    LOGIN_URL_TEMPLATE = (
        "https://xui.ptlogin2.qq.com/cgi-bin/xlogin?"
        "pt_enable_pwd=1&appid=716027609&pt_3rd_aid=102061775&daid=381"
        "&pt_skey_valid=0&style=35&force_qr=1&autorefresh=1"
        "&s_url=http%3A%2F%2Fconnect.qq.com&refer_cgi=m_authorize&ucheck=1"
        "&fall_to_wv=1&status_os=12&redirect_uri=auth%3A%2F%2Ftauth.qq.com%2F"
        "&client_id=102061775&pf=openmobile_android&response_type=token&scope=all"
        "&sdkp=a&sdkv=3.5.17.lite&sign=a6479455d3e49b597350f13f776a6288"
        "&status_machine=MjMxMTdSSzY2Qw%3D%3D&switch=1&time=1763280194"
        "&show_download_ui=true&h5sig=trobryxo8IPM0GaSQH12mowKG-CY65brFzkK7_-9EW4&loginty=6"
    )
    PTQR_SHOW_URL = "https://xui.ptlogin2.qq.com/ssl/ptqrshow"
    PTQR_LOGIN_URL = "https://xui.ptlogin2.qq.com/ssl/ptqrlogin"
    OPENMOBILE_REDIRECT_URL = "https://openmobile.qq.com/oauth2.0/m_get_redirect_url"
    PTQR_AID = "716027609"
    PTQR_DAID = "381"
    PTQR_THIRD_AID = "102061775"
    DEFAULT_LOGIN_CALLBACK_URL = "http://connect.qq.com"
    DEFAULT_LOGIN_U1_URL = "http://connect.qq.com"

    # 微信登录配置
    WECHAT_APP_ID = "wxcbb49f1f39656c2a"

    STORE_URL = "https://app.mval.qq.com/go/mlol_store/agame/user_store"

    def __init__(self):
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        TEMP_DIR.mkdir(parents=True, exist_ok=True)
        self.cleanup_temp_files(stale_only=True)

    def cleanup_temp_files(self, stale_only: bool = False) -> None:
        now = time.time()
        for path in TEMP_DIR.iterdir():
            if not path.is_file() or path.name == "merged.jpg":
                continue
            is_generated = (
                path.name.startswith(("bg_", "goods_", "qq_login_", "wechat_login_"))
                or path.suffix.lower() in {".jpg", ".jpeg", ".png"}
            )
            if not is_generated:
                continue
            if stale_only and now - path.stat().st_mtime < 86400:
                continue
            try:
                path.unlink()
            except OSError:
                pass

    def _remove_old_qr_files(self) -> None:
        """重新生成二维码前清掉历史二维码，避免临时目录堆积。"""
        for path in TEMP_DIR.glob("*_login_*.png"):
            try:
                path.unlink()
            except OSError:
                pass

    # ---------- 基础工具 ----------

    def _normalize_url(self, value: str, default: str = "") -> str:
        url = (value or default or "").strip()
        if not url:
            return ""
        if not re.match(r"^https?://", url, re.IGNORECASE):
            url = f"https://{url.lstrip('/')}"
        return url

    def _get_login_callback_url(self) -> str:
        return self.DEFAULT_LOGIN_CALLBACK_URL

    def _get_login_u1_url(self, callback_url: str) -> str:
        return self.DEFAULT_LOGIN_U1_URL

    def _build_login_url(self, callback_url: str) -> str:
        encoded_callback = urllib.parse.quote(callback_url, safe="")
        if "s_url=" not in self.LOGIN_URL_TEMPLATE:
            return self.LOGIN_URL_TEMPLATE
        return re.sub(
            r"([?&])s_url=[^&]*",
            lambda m: f"{m.group(1)}s_url={encoded_callback}",
            self.LOGIN_URL_TEMPLATE,
            count=1,
        )

    def _get_cookie_value(self, session: aiohttp.ClientSession, url: str, name: str) -> str:
        try:
            cookies = session.cookie_jar.filter_cookies(url)
            cookie = cookies.get(name)
            if cookie:
                return cookie.value
        except Exception as e:
            logger.warning("读取 Cookie 失败：%s %s", name, e)
        return ""

    def _calc_ptqrtoken(self, qrsig: str) -> int:
        token = 0
        for ch in qrsig:
            token += (token << 5) + ord(ch)
        return token & 2147483647

    def _parse_ptui_callback(self, text: str) -> Optional[Dict[str, str]]:
        match = re.search(r"ptuiCB\('([^']*)','([^']*)','([^']*)','([^']*)','([^']*)'", text)
        if not match:
            return None
        redirect_url = match.group(3).replace("\\/", "/").replace("\\x26", "&")
        return {
            "code": match.group(1),
            "redirect_url": redirect_url,
            "message": match.group(5),
        }

    def _extract_login_data_from_success_url(self, success_url: str) -> Dict[str, Any]:
        def normalize_url(url: str) -> str:
            return (url or "").replace("\\/", "/").replace("\\x26", "&").strip()

        def parse_param_str(raw: str) -> Dict[str, str]:
            parsed: Dict[str, str] = {}
            if not raw:
                return parsed
            part = raw.replace("#&", "&").lstrip("&")
            for key, value in urllib.parse.parse_qs(part, keep_blank_values=True).items():
                if value:
                    parsed[key] = value[0]
            return parsed

        nested_keys = {
            "u1", "url", "jump_url", "redirect_uri", "redirect_url",
            "target_url", "s_url", "f_url", "qtarget", "jump", "ru",
        }

        merged_params: Dict[str, str] = {}
        queue = [normalize_url(success_url)]
        visited = set()

        while queue:
            candidate = queue.pop(0)
            if not candidate or candidate in visited:
                continue
            visited.add(candidate)

            decoded = candidate
            for _ in range(3):
                next_decoded = urllib.parse.unquote(decoded)
                if next_decoded == decoded:
                    break
                decoded = next_decoded

            parsed_url = urllib.parse.urlparse(decoded)
            candidate_params: Dict[str, str] = {}
            for raw_part in (parsed_url.query, parsed_url.fragment):
                candidate_params.update(parse_param_str(raw_part))

            if not candidate_params and ("openid=" in decoded or "access_token=" in decoded):
                candidate_params.update(parse_param_str(decoded))

            for key, value in candidate_params.items():
                if key not in merged_params:
                    merged_params[key] = value

            for nested_key in nested_keys:
                nested_value = candidate_params.get(nested_key, "")
                if nested_value and nested_value not in visited:
                    queue.append(normalize_url(nested_value))

        return {
            "openid": merged_params.get("openid", ""),
            "appid": merged_params.get("appid", ""),
            "access_token": merged_params.get("access_token", ""),
            "pay_token": merged_params.get("pay_token", ""),
            "key": merged_params.get("key", ""),
            "redirect_uri_key": merged_params.get("redirect_uri_key", ""),
            "expires_in": merged_params.get("expires_in", "7776000"),
            "pf": merged_params.get("pf", "openmobile_android"),
            "status_os": merged_params.get("status_os", "12"),
            "status_machine": merged_params.get("status_machine", ""),
            "full_params": merged_params,
        }

    def _build_pt_openlogin_data(self, login_url: str, session: aiohttp.ClientSession) -> str:
        parsed = urllib.parse.urlparse(login_url)
        query_map = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)

        def q(name: str, default: str = "") -> str:
            values = query_map.get(name, [])
            return values[0] if values else default

        tid = self._get_cookie_value(session, "https://xui.ptlogin2.qq.com", "idt") or str(int(time.time()))
        auth_time = str(int(time.time() * 1000))
        items = [
            ("which", ""),
            ("refer_cgi", q("refer_cgi", "m_authorize")),
            ("response_type", q("response_type", "token")),
            ("client_id", q("client_id", self.PTQR_THIRD_AID)),
            ("state", ""),
            ("display", ""),
            ("openapi", "1011"),
            ("switch", q("switch", "1")),
            ("src", "1"),
            ("sdkv", q("sdkv", "3.5.17.lite")),
            ("sdkp", q("sdkp", "a")),
            ("tid", tid),
            ("pf", q("pf", "openmobile_android")),
            ("need_pay", "0"),
            ("browser", "0"),
            ("browser_error", ""),
            ("serial", ""),
            ("token_key", ""),
            ("redirect_uri", q("redirect_uri", "auth://tauth.qq.com/")),
            ("sign", q("sign", "")),
            ("time", q("time", "")),
            ("status_version", ""),
            ("status_os", q("status_os", "12")),
            ("status_machine", q("status_machine", "")),
            ("page_type", "1"),
            ("has_auth", "1"),
            ("update_auth", "1"),
            ("auth_time", auth_time),
            ("loginfrom", ""),
            ("h5sig", q("h5sig", "")),
            ("loginty", q("loginty", "6")),
        ]
        return urllib.parse.urlencode(items)

    def _extract_jsver_from_login_page(self, login_page: str) -> str:
        text = login_page or ""
        patterns = [
            r"/monorepo/([0-9A-Za-z]+)/ptlogin/js/login_10\.js",
            r"/monorepo/([0-9A-Za-z]+)/ptlogin/js/",
            r"https://qq-web\.cdn-go\.cn/monorepo/([0-9A-Za-z]+)",
        ]
        for pattern in patterns:
            m = re.search(pattern, text)
            if m:
                return m.group(1)
        return "28d22679"

    def _build_aegis_uid(self, session: aiohttp.ClientSession) -> str:
        aegis_uid = self._get_cookie_value(session, "https://xui.ptlogin2.qq.com", "__aegis_uid")
        if aegis_uid:
            return aegis_uid
        server_ip = self._get_cookie_value(session, "https://xui.ptlogin2.qq.com", "pt_serverip")
        client_ip = self._get_cookie_value(session, "https://xui.ptlogin2.qq.com", "pt_clientip")
        if server_ip and client_ip:
            return f"{server_ip}-{client_ip}-4458"
        return ""

    def _extract_auth_url_from_callback_body(self, text: str) -> str:
        if not text:
            return ""
        callback_match = re.search(r"_Callback\s*\(\s*(\{.*?\})\s*\)\s*;?\s*$", text, re.DOTALL)
        if callback_match:
            payload_text = callback_match.group(1)
            try:
                payload = json.loads(payload_text)
                callback_url = str(payload.get("url", "") or "").strip()
                if callback_url.startswith("auth://"):
                    return callback_url
            except Exception:
                pass

        auth_match = re.search(r"(auth://tauth\.qq\.com/[^\s\"'<>]+)", text)
        if auth_match:
            return auth_match.group(1)
        return ""

    def _merge_login_data(self, base_data: Dict[str, Any], extra_data: Dict[str, Any]) -> Dict[str, Any]:
        base = dict(base_data or {})
        extra = dict(extra_data or {})
        merged_params: Dict[str, str] = dict(base.get("full_params", {}) or {})
        merged_params.update(extra.get("full_params", {}) or {})

        for key in (
            "openid", "appid", "access_token", "pay_token", "key",
            "redirect_uri_key", "expires_in", "pf", "status_os", "status_machine",
        ):
            if not base.get(key) and extra.get(key):
                base[key] = extra[key]
        base["full_params"] = merged_params
        return base

    def _collect_redirect_key_candidates(
        self,
        session: aiohttp.ClientSession,
        login_data: Dict[str, Any],
        success_url: str,
    ) -> list:
        result = []
        seen = set()

        def add_key(value: str, source: str):
            keystr = (value or "").strip()
            if not keystr or keystr in seen:
                return
            seen.add(keystr)
            result.append((keystr, source))

        full_params = (login_data or {}).get("full_params", {}) or {}
        for key_name in ("redirect_uri_key", "keystr", "key", "uikey", "superkey", "supertoken"):
            add_key(str(full_params.get(key_name, "")), f"param:{key_name}")

        normalized_url = (success_url or "").replace("\\/", "/").replace("\\x26", "&")
        parsed = urllib.parse.urlparse(normalized_url)
        raw_parts = [parsed.query, parsed.fragment]
        if not parsed.query and not parsed.fragment:
            raw_parts.append(normalized_url)

        for raw in raw_parts:
            if not raw:
                continue
            raw_params = urllib.parse.parse_qs(raw.replace("#&", "&"), keep_blank_values=True)
            for key_name in ("redirect_uri_key", "keystr", "key", "uikey", "superkey", "supertoken"):
                values = raw_params.get(key_name, [])
                if values:
                    add_key(values[0], f"url:{key_name}")

        cookie_domains = [
            "https://xui.ptlogin2.qq.com",
            "https://ssl.ptlogin2.qq.com",
            "https://ptlogin4.openmobile.qq.com",
            "https://openmobile.qq.com",
            "https://connect.qq.com",
        ]
        for domain in cookie_domains:
            host = urllib.parse.urlparse(domain).netloc
            for key_name in ("redirect_uri_key", "keystr", "uikey", "superkey", "supertoken", "key"):
                add_key(self._get_cookie_value(session, domain, key_name), f"cookie:{host}:{key_name}")

        return result

    async def _fetch_auth_url_by_redirect_key(self, session: aiohttp.ClientSession, redirect_uri_key: str) -> str:
        keystr = (redirect_uri_key or "").strip()
        if not keystr:
            return ""

        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Linux; Android 12; 23117RK66C Build/V417IR; wv) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 "
                "Chrome/101.0.4951.61 Mobile Safari/537.36 tencent_game_emulator"
            ),
            "Accept": "*/*",
            "Referer": "https://imgcache.qq.com/",
        }
        try:
            async with session.get(
                self.OPENMOBILE_REDIRECT_URL,
                params={"keystr": keystr},
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=20, connect=10, sock_connect=10, sock_read=15),
            ) as response:
                body = await response.text(errors="ignore")
                if response.status != 200:
                    return ""
                return self._extract_auth_url_from_callback_body(body)
        except Exception as e:
            logger.warning("m_get_redirect_url 异常：%s", e)
            return ""

    async def _resolve_login_success_url(
        self,
        session: aiohttp.ClientSession,
        success_url: str,
        referer_url: str = "",
    ) -> str:
        current_url = (success_url or "").replace("\\/", "/").replace("\\x26", "&").strip()
        if not current_url:
            return ""
        if "check_sig" not in current_url:
            return current_url

        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Linux; Android 12; 23117RK66C Build/V417IR; wv) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 "
                "Chrome/101.0.4951.61 Mobile Safari/537.36 tencent_game_emulator"
            ),
            "Accept": "*/*",
            "Referer": referer_url or "https://openmobile.qq.com/",
        }
        try:
            async with session.get(
                current_url,
                headers=headers,
                allow_redirects=False,
                timeout=aiohttp.ClientTimeout(total=15, connect=8, sock_connect=8, sock_read=10),
            ) as response:
                body = await response.text(errors="ignore")
                location = (response.headers.get("Location", "") or "").strip()
                if location:
                    return urllib.parse.urljoin(str(response.url), location)

                body_url = self._extract_url_from_body(body)
                if body_url:
                    return body_url
        except Exception as e:
            logger.warning("check_sig 解析异常：%s", e)
        return current_url

    def _extract_url_from_body(self, body: str) -> str:
        text = (body or "").replace("\\/", "/").replace("\\x26", "&")
        patterns = [
            r"ptuiCB\('[^']*','[^']*','([^']+)'",
            r"ptui_auth_CB\('[^']*','[^']*','([^']+)'",
            r"location\.href\s*=\s*['\"]([^'\"]+)['\"]",
            r"location\.replace\(\s*['\"]([^'\"]+)['\"]\s*\)",
            r"window\.location\s*=\s*['\"]([^'\"]+)['\"]",
            r"(auth://tauth\.qq\.com/[^\s\"'<>]+)",
            r"(https?://imgcache\.qq\.com/[^\s\"'<>]+)",
        ]
        for pattern in patterns:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                return match.group(1).strip()
        return ""

    # ---------- QQ 登录 ----------

    async def generate_qq_qr_code(self) -> Optional[Dict[str, Any]]:
        logger.info("[QQ登录] 开始生成二维码")
        session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20))
        callback_url = self._get_login_callback_url()
        u1_url = self._get_login_u1_url(callback_url)
        login_url = self._build_login_url(callback_url)

        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Linux; Android 12; 23117RK66C Build/V417IR; wv) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/101.0.4951.61 Mobile Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Referer": "https://openmobile.qq.com/",
            "X-Requested-With": "com.tencent.apps.valorant",
            "Cookie": "accountType=5; clientType=9",
        }

        try:
            async with session.get(login_url, headers=headers) as response:
                response.raise_for_status()
                login_page = await response.text(errors="ignore")

            login_sig = ""
            login_sig_match = re.search(r"g_login_sig=encodeURIComponent\(\"([^\"]+)\"\)", login_page)
            if login_sig_match:
                login_sig = login_sig_match.group(1)
            if not login_sig:
                login_sig = self._get_cookie_value(session, "https://xui.ptlogin2.qq.com", "pt_login_sig")
            if not login_sig:
                login_sig = self._get_cookie_value(session, "https://ssl.ptlogin2.qq.com", "pt_login_sig")

            parsed_login_url = urllib.parse.urlparse(login_url)
            login_query_map = urllib.parse.parse_qs(parsed_login_url.query, keep_blank_values=True)
            login_u1 = u1_url
            pt_uistyle = login_query_map.get("style", ["35"])[0] or "35"
            ptlang = login_query_map.get("ptlang", ["2052"])[0] or "2052"
            jsver = self._extract_jsver_from_login_page(login_page)
            pt_openlogin_data = self._build_pt_openlogin_data(login_url, session)
            aegis_uid = self._build_aegis_uid(session)

            qr_params = {
                "s": "8",
                "e": "0",
                "appid": self.PTQR_AID,
                "type": "0",
                "t": str(random.random()),
                "u1": login_u1,
                "daid": self.PTQR_DAID,
                "pt_3rd_aid": self.PTQR_THIRD_AID,
            }
            qr_headers = {
                "User-Agent": headers["User-Agent"],
                "Referer": login_url,
                "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
                "X-Requested-With": "com.tencent.apps.valorant",
            }
            async with session.get(self.PTQR_SHOW_URL, params=qr_params, headers=qr_headers) as response:
                response.raise_for_status()
                qr_image_bytes = await response.read()

            if not qr_image_bytes:
                raise RuntimeError("二维码内容为空")

            qrsig = self._get_cookie_value(session, "https://xui.ptlogin2.qq.com", "qrsig")
            if not qrsig:
                qrsig = self._get_cookie_value(session, "https://ssl.ptlogin2.qq.com", "qrsig")
            if not qrsig:
                raise RuntimeError("未获取到 qrsig")

            qr_path = TEMP_DIR / f"qq_login_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
            self._remove_old_qr_files()
            qr_path.write_bytes(qr_image_bytes)

            logger.info("[QQ登录] 二维码已生成：%s", qr_path)
            return {
                "session": session,
                "qr_path": str(qr_path),
                "ptqrtoken": self._calc_ptqrtoken(qrsig),
                "login_sig": login_sig,
                "login_url": login_url,
                "u1_url": login_u1,
                "pt_openlogin_data": pt_openlogin_data,
                "aegis_uid": aegis_uid,
                "jsver": jsver,
                "pt_uistyle": pt_uistyle,
                "ptlang": ptlang,
            }
        except Exception as e:
            logger.error("[QQ登录] 生成二维码失败：%s", e)
            await session.close()
            return None

    async def wait_for_qq_login_result(
        self,
        session: aiohttp.ClientSession,
        ptqrtoken: int,
        login_sig: str,
        login_u1: str,
        referer_url: str,
        pt_openlogin_data: str = "",
        aegis_uid: str = "",
        jsver: str = "28d22679",
        pt_uistyle: str = "35",
        ptlang: str = "2052",
        timeout: int = 90,
    ) -> Optional[Dict[str, Any]]:
        logger.info("[QQ登录] 请扫码，等待登录结果，最长 %s 秒", timeout)

        poll_headers = {
            "User-Agent": (
                "Mozilla/5.0 (Linux; Android 12; 23117RK66C Build/V417IR; wv) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 "
                "Chrome/101.0.4951.61 Mobile Safari/537.36 tencent_game_emulator"
            ),
            "Referer": referer_url,
            "Accept": "*/*",
            "X-Requested-With": "com.tencent.apps.valorant",
        }

        start_time = time.time()
        poll_index = 0
        while time.time() - start_time < timeout:
            poll_index += 1
            try:
                params = {
                    "u1": login_u1,
                    "from_ui": "1",
                    "type": "1",
                    "ptlang": str(ptlang or "2052"),
                    "ptqrtoken": str(ptqrtoken),
                    "daid": self.PTQR_DAID,
                    "aid": self.PTQR_AID,
                    "pt_3rd_aid": self.PTQR_THIRD_AID,
                    "pt_openlogin_data": pt_openlogin_data,
                    "device": "2",
                    "ptopt": "1",
                    "pt_uistyle": str(pt_uistyle or "35"),
                    "jsver": str(jsver or "28d22679"),
                    "r": str(random.random()),
                }
                if login_sig:
                    params["login_sig"] = login_sig
                if aegis_uid:
                    params["aegis_uid"] = aegis_uid

                async with session.get(self.PTQR_LOGIN_URL, params=params, headers=poll_headers) as response:
                    response.raise_for_status()
                    text = await response.text(errors="ignore")

                callback = self._parse_ptui_callback(text)
                if not callback:
                    logger.warning("[QQ登录] 无法解析回调：%s", text[:160])
                    await asyncio.sleep(2)
                    continue

                code = callback["code"]
                message = callback["message"]
                redirect_url = callback.get("redirect_url", "")

                if code == "0":
                    logger.info("[QQ登录] 扫码成功，正在换取 token")
                    success_url = redirect_url
                    login_data = self._extract_login_data_from_success_url(success_url)
                    if not (login_data.get("openid") and login_data.get("access_token")):
                        resolved_url = await self._resolve_login_success_url(
                            session=session,
                            success_url=success_url,
                            referer_url=referer_url,
                        )
                        if resolved_url and resolved_url != success_url:
                            resolved_data = self._extract_login_data_from_success_url(resolved_url)
                            login_data = self._merge_login_data(login_data, resolved_data)

                        candidate_url = resolved_url if resolved_url else success_url
                        key_candidates = self._collect_redirect_key_candidates(
                            session=session,
                            login_data=login_data,
                            success_url=candidate_url,
                        )
                        for keystr, source in key_candidates:
                            auth_url = await self._fetch_auth_url_by_redirect_key(session, keystr)
                            if not auth_url:
                                continue
                            auth_data = self._extract_login_data_from_success_url(auth_url)
                            login_data = self._merge_login_data(login_data, auth_data)
                            if login_data.get("openid") and login_data.get("access_token"):
                                break

                    if login_data.get("openid") and login_data.get("access_token"):
                        logger.info("[QQ登录] 已拿到 openid/access_token")
                        return login_data

                    logger.error("[QQ登录] 登录成功但缺少 openid/access_token")
                    return None

                if code == "65":
                    logger.error("[QQ登录] 二维码已失效：%s", message)
                    return None

                if code in ("66", "67"):
                    logger.info("[QQ登录] 等待扫码/确认：%s", message)
                    await asyncio.sleep(2)
                    continue

                logger.warning("[QQ登录] 状态异常 code=%s message=%s", code, message)
                await asyncio.sleep(2)

            except Exception as e:
                logger.warning("[QQ登录] 轮询异常：%s", e)
                await asyncio.sleep(2)

        logger.error("[QQ登录] 轮询超时")
        return None

    async def qq_login(self) -> Optional[Dict[str, Any]]:
        ctx = await self.generate_qq_qr_code()
        if not ctx:
            return None

        http_session: aiohttp.ClientSession = ctx["session"]
        qr_path = ctx["qr_path"]
        try:
            print("\nQQ 登录二维码已生成：")
            print(qr_path)
            print("请打开这张图片扫码登录。Windows 可直接双击打开；OpenWrt 可把图片下载到手机查看。\n")

            login_data = await self.wait_for_qq_login_result(
                session=http_session,
                ptqrtoken=ctx["ptqrtoken"],
                login_sig=ctx.get("login_sig", ""),
                login_u1=ctx.get("u1_url", self.DEFAULT_LOGIN_U1_URL),
                referer_url=ctx.get("login_url", self._build_login_url(self.DEFAULT_LOGIN_CALLBACK_URL)),
                pt_openlogin_data=ctx.get("pt_openlogin_data", ""),
                aegis_uid=ctx.get("aegis_uid", ""),
                jsver=ctx.get("jsver", "28d22679"),
                pt_uistyle=ctx.get("pt_uistyle", "35"),
                ptlang=ctx.get("ptlang", "2052"),
                timeout=LOGIN_TIMEOUT_SECONDS,
            )
            if not login_data:
                return None

            final_data = await self.get_final_cookies_by_qq(login_data)
            return final_data
        finally:
            await http_session.close()

    async def get_final_cookies_by_qq(self, login_data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        openid = login_data.get("openid", "")
        access_token = login_data.get("access_token", "")

        if not openid or not access_token:
            logger.error("缺少 openid 或 access_token")
            return None

        login_url = "https://app.mval.qq.com/go/auth/login_by_qq?source_game_zone=agame&game_zone=agame"

        headers = {
            "Cookie": "clientType=9; openid=null; access_token=null;",
            "User-Agent": (
                "mval/2.4.0.10053 Channel/10068 Manufacturer/Redmi "
                "Mozilla/5.0 (Linux; Android 12; 23117RK66C Build/V417IR; wv) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 "
                "Chrome/101.0.4951.61 Mobile Safari/537.36"
            ),
            "Content-Type": "application/json",
            "Host": "app.mval.qq.com",
            "Connection": "Keep-Alive",
            "Accept-Encoding": "gzip",
        }

        data = {
            "clienttype": 9,
            "config_params": {
                "client_dev_name": "23117RK66C",
                "lang_type": 0,
            },
            "login_info": {
                "appid": 102061775,
                "openid": openid,
                "qq_info_type": 5,
                "sig": access_token,
                "uin": 0,
            },
            "mappid": 10200,
            "mcode": "132f0a77d34402abc8463d60100011d19b0e",
            "source_game_zone": "agame",
            "game_zone": "agame",
        }

        try:
            async with aiohttp.ClientSession(trust_env=True) as session:  # 服务器版唯一改动（R1-对策5）：信任 HTTPS_PROXY
                async with session.post(login_url, headers=headers, json=data) as response:
                    response.raise_for_status()
                    result = await response.json(content_type=None)

            if result.get("result") == 0:
                login_info = result.get("data", {}).get("login_info", {})
                uin = login_info.get("uin", 0)
                user_id = login_info.get("user_id", "")
                wt = login_info.get("wt", "")

                final_cookie = (
                    f"clientType=9; "
                    f"uin=o{uin}; "
                    f"appid=102061775; "
                    f"acctype=qc; "
                    f"openid={openid}; "
                    f"access_token=null; "
                    f"userId={user_id}; "
                    f"accountType=5; "
                    f"tid={wt};"
                )

                logger.info("[QQ登录] 登录成功 userId=%s", _mask_user_id(user_id))
                return {
                    "userId": user_id,
                    "tid": wt,
                    "openid": openid,
                    "uin": uin,
                    "login_type": "qq",
                    "final_cookie": final_cookie,
                    "saved_at": datetime.now().isoformat(timespec="seconds"),
                }

            logger.error("[QQ登录] 获取最终 Cookie 失败：%s", result.get("msg", result))
            return None
        except Exception as e:
            logger.error("[QQ登录] 获取最终 Cookie 出错：%s", e)
            return None

    # ---------- 微信登录 ----------

    async def generate_wechat_qr_code(self) -> Optional[Dict[str, Any]]:
        logger.info("[微信登录] 开始生成二维码")
        url = "https://open.weixin.qq.com/connect/sdk/qrconnect?f=json"

        timestamp = str(int(time.time()))
        noncestr = "".join(random.choices(string.ascii_letters + string.digits, k=6))

        try:
            async with aiohttp.ClientSession(trust_env=True) as session:  # 服务器版唯一改动（R1-对策5）：信任 HTTPS_PROXY
                ticket_url = "https://app.mval.qq.com/go/auth/get_sdk_ticket"
                ticket_payload = {
                    "clienttype": 9,
                    "config_params": {"client_dev_name": "22041216C", "lang_type": 0},
                    "mappid": 10200,
                    "mcode": "69028af6dca2c107f4f58290100011b1a303",
                    "sdk_appid": self.WECHAT_APP_ID,
                    "source_game_zone": "agame",
                    "game_zone": "agame",
                }
                ticket_headers = {
                    "user-agent": (
                        "mval/2.10062 Channel/3 Manufacturer/Redmi "
                        "Mozilla/5.0 (Linux; Android 12; 22041216C Build/V417IR; wv) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 "
                        "Chrome/110.0.5481.154 Mobile Safari/537.36"
                    ),
                    "content-type": "application/json",
                }

                async with session.post(ticket_url, headers=ticket_headers, json=ticket_payload) as resp:
                    ticket_resp = await resp.json(content_type=None)

                sdk_ticket = ticket_resp.get("data", {}).get("ticket", "")
                if not sdk_ticket:
                    logger.error("[微信登录] 获取 sdk_ticket 失败：%s", ticket_resp)
                    return None

                raw_string = f"appid={self.WECHAT_APP_ID}&noncestr={noncestr}&sdk_ticket={sdk_ticket}&timestamp={timestamp}"
                signature = hashlib.sha1(raw_string.encode("utf-8")).hexdigest()

                params = {
                    "appid": self.WECHAT_APP_ID,
                    "noncestr": noncestr,
                    "timestamp": timestamp,
                    "scope": "snsapi_userinfo",
                    "signature": signature,
                }
                headers = {
                    "User-Agent": (
                        "mval/2.10053 Channel/10068 Manufacturer/Redmi "
                        "Mozilla/5.0 (Linux; Android 12; 23117RK66C Build/V417IR; wv) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 "
                        "Chrome/101.0.4951.61 Mobile Safari/537.36"
                    ),
                    "Content-Type": "application/json",
                    "Accept": "*/*",
                }

                async with session.get(url, params=params, headers=headers) as resp:
                    resp_text = await resp.text()
                    try:
                        result = await resp.json(content_type=None)
                    except Exception:
                        result = json.loads(resp_text)

                if result.get("errcode") != 0:
                    logger.error("[微信登录] 获取二维码失败：%s", result)
                    return None

                uuid = result.get("uuid")
                qrcode_base64 = result.get("qrcode", {}).get("qrcodebase64", "")

                if not uuid or not qrcode_base64:
                    logger.error("[微信登录] 二维码数据不完整：%s", result)
                    return None

                if "," in qrcode_base64:
                    qrcode_base64 = qrcode_base64.split(",", 1)[1]
                qr_image_bytes = base64.b64decode(qrcode_base64)

                qr_path = TEMP_DIR / f"wechat_login_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
                self._remove_old_qr_files()
                qr_path.write_bytes(qr_image_bytes)

            return {"uuid": uuid, "qr_path": str(qr_path)}

        except Exception as e:
            logger.error("[微信登录] 异常：%s", e)
            return None

    async def wechat_login(self) -> Optional[Dict[str, Any]]:
        ctx = await self.generate_wechat_qr_code()
        if not ctx:
            return None
        qr_path = ctx["qr_path"]
        print("\n微信登录二维码已生成：")
        print(qr_path)
        print(f"请打开这张图片，用微信扫码确认。最长等待 {LOGIN_TIMEOUT_SECONDS} 秒。\n")
        return await self.wait_for_wechat_login_result(ctx["uuid"])

    async def wait_for_wechat_login_result(
        self, uuid: str, timeout: int = LOGIN_TIMEOUT_SECONDS
    ) -> Optional[Dict[str, Any]]:
        logger.info("[微信登录] 等待扫码 uuid=%s", uuid)
        try:
            async with aiohttp.ClientSession(trust_env=True) as session:  # 服务器版唯一改动（R1-对策5）：信任 HTTPS_PROXY
                headers = {
                    "User-Agent": (
                        "mval/2.10053 Channel/10068 Manufacturer/Redmi "
                        "Mozilla/5.0 (Linux; Android 12; 23117RK66C Build/V417IR; wv) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 "
                        "Chrome/101.0.4951.61 Mobile Safari/537.36"
                    ),
                    "Content-Type": "application/json",
                    "Accept": "*/*",
                }

                wx_code = None
                last_code = None
                for _ in range(max(1, timeout // 2)):
                    await asyncio.sleep(2)

                    poll_url = f"https://long.open.weixin.qq.com/connect/l/qrconnect?f=json&uuid={uuid}"
                    if last_code is not None:
                        poll_url += f"&last={last_code}"

                    async with session.get(poll_url, headers=headers) as resp:
                        resp_text = await resp.text()
                        try:
                            result = await resp.json(content_type=None)
                        except Exception:
                            if "window.wx_errcode" in resp_text:
                                errcode_match = re.search(r"wx_errcode=(\d+)", resp_text)
                                code_match = re.search(r"wx_code='([^']+)'", resp_text)
                                result = {
                                    "wx_errcode": int(errcode_match.group(1)) if errcode_match else 408,
                                    "wx_code": code_match.group(1) if code_match else "",
                                }
                            else:
                                try:
                                    result = json.loads(resp_text)
                                except Exception:
                                    result = {}

                    wx_errcode = result.get("wx_errcode")
                    last_code = wx_errcode

                    if wx_errcode in [0, 405] and result.get("wx_code"):
                        logger.info("[微信登录] 扫码成功")
                        wx_code = result.get("wx_code")
                        break
                    elif wx_errcode == 404:
                        logger.info("[微信登录] 已扫码，等待确认")
                        continue
                    elif wx_errcode == 408:
                        logger.info("[微信登录] 等待扫码")
                        continue
                    else:
                        logger.warning("[微信登录] 状态：%s", result)

                if not wx_code:
                    logger.error("[微信登录] 等待超时或未获取 wx_code")
                    return None

                login_url = "https://app.mval.qq.com/go/auth/login_by_wechat"
                payload = {
                    "clienttype": 9,
                    "config_params": {
                        "client_dev_name": "22041216C",
                        "lang_type": 0,
                    },
                    "login_info": {
                        "appid": self.WECHAT_APP_ID,
                        "check_third_type": 1,
                        "code": wx_code,
                        "wx_info_type": 1,
                    },
                    "mappid": 10200,
                    "mcode": "69028af6dca2c107f4f58290100011b1a303",
                    "source_game_zone": "agame",
                    "game_zone": "agame",
                }
                login_headers = {
                    "user-agent": (
                        "mval/2.6.0.10062 Channel/3 Manufacturer/Redmi "
                        "Mozilla/5.0 (Linux; Android 12; 22041216C Build/V417IR; wv) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 "
                        "Chrome/110.0.5481.154 Mobile Safari/537.36"
                    ),
                    "content-type": "application/json",
                    "cookie": "clientType=9; openid=null; access_token=null;",
                }
                async with session.post(login_url, headers=login_headers, json=payload) as resp:
                    login_result = await resp.json(content_type=None)

                login_info = login_result.get("data", {}).get("login_info", {})
                if login_info and login_info.get("result") == 0:
                    user_id = login_info.get("user_id")
                    wt = login_info.get("wt")
                    logger.info("[微信登录] 登录成功 userId=%s", _mask_user_id(user_id))
                    return {
                        "userId": user_id,
                        "tid": wt,
                        "openid": login_info.get("openid"),
                        "access_token": login_info.get("access_token"),
                        "clienttype": 9,
                        "login_type": "wechat",
                        "saved_at": datetime.now().isoformat(timespec="seconds"),
                    }

                logger.error("[微信登录] 验证失败：%s", login_result)
                return None

        except Exception as e:
            logger.error("[微信登录] 轮询异常：%s", e)
            return None

    # ---------- 配置读写 ----------

    def load_config(self) -> Optional[Dict[str, Any]]:
        if not CONFIG_FILE.exists():
            return None
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            if data.get("userId") and data.get("tid"):
                return data
        except Exception as e:
            logger.error("读取 config.json 失败：%s", e)
        return None

    def save_config(self, data: Dict[str, Any]) -> None:
        CONFIG_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        AUTH_EXPIRED_STATE_FILE.unlink(missing_ok=True)
        for qr_path in TEMP_DIR.glob("*_login_*.png"):
            qr_path.unlink(missing_ok=True)
        logger.info("登录配置已保存：%s", CONFIG_FILE)

    def check_mail_config(self, strict: bool = True) -> bool:
        missing = []
        if not MAIL_USER:
            missing.append("MAIL_USER")
        if not MAIL_PASS:
            missing.append("MAIL_PASS")

        if missing:
            msg = (
                "邮箱配置未填写完整："
                + ", ".join(missing)
                + "。请打开脚本顶部配置区填写，或设置环境变量。"
            )
            if strict:
                raise RuntimeError(msg)
            logger.warning(msg)
            return False
        return True

    # ---------- 商店查询 ----------

    def _build_store_api_headers(self, user_config: Dict[str, Any]) -> Dict[str, str]:
        final_cookie = str(user_config.get("final_cookie") or "").strip()
        if not final_cookie:
            cookie_values = {
                "clientType": user_config.get("clienttype", 9),
                "uin": f"o{user_config['uin']}" if user_config.get("uin") else None,
                "appid": 102061775 if user_config.get("login_type") == "qq" else None,
                "acctype": "qc" if user_config.get("login_type") == "qq" else None,
                "openid": user_config.get("openid"),
                "access_token": user_config.get("access_token"),
                "userId": user_config["userId"],
                "accountType": 5,
                "tid": user_config["tid"],
            }
            final_cookie = "; ".join(
                f"{key}={value}"
                for key, value in cookie_values.items()
                if value is not None and value != ""
            )

        return {
            "Accept": "*/*",
            "Upload-Draft-Interop-Version": "5",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Accept-Encoding": "gzip, deflate, br",
            "Content-Type": "application/json",
            "User-Agent": (
                "mval/2.3.0.10050 Channel/5 Manufacturer/Xiaomi "
                "Mozilla/5.0 (Linux; Android 14; 23078RKD5C Build/UP1A.230905.011; wv) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 "
                "Chrome/140.0.7339.207 Mobile Safari/537.36"
            ),
            "Connection": "keep-alive",
            "Upload-Complete": "?1",
            "GH-HEADER": "1-2-105-160-0",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
            "Cookie": final_cookie,
        }

    def _get_store_api_error_message(self, response_data: Dict[str, Any]) -> str:
        return response_data.get("errMsg") or response_data.get("msg") or "未知错误"

    def _is_store_auth_invalid(self, result_code: Any, err_msg: str) -> bool:
        err_msg_lower = (err_msg or "").lower()
        return (
            result_code in {1001, 1003, 999999}
            or "ticket expire" in err_msg_lower
            or "auth web ticket fail" in err_msg_lower
        )

    def _extract_shop_goods_list(self, response_data: Dict[str, Any]) -> Tuple[Optional[list], Optional[str]]:
        if "data" not in response_data:
            return None, "商店接口返回格式异常：缺少 data"

        if not response_data["data"]:
            return [], None

        data = response_data["data"]
        if isinstance(data, list):
            data = data[0] if data else {}

        if not isinstance(data, dict):
            return None, "商店接口返回格式异常：data 不是对象"

        goods_list = data.get("list", [])
        if not goods_list:
            return [], None

        return goods_list, None

    async def request_store_api(
        self,
        user_config: Dict[str, Any],
        timeout: int = 15,
    ) -> Tuple[Optional[Dict[str, Any]], Optional[str], bool]:
        if not all(k in user_config for k in ["userId", "tid"]):
            return None, "配置不完整，需要包含 userId 和 tid", False

        headers = self._build_store_api_headers(user_config)

        attempt = 0

        while True:
            attempt += 1
            timestamp = int(time.time())
            data = {"_t": timestamp}
            try:
                logger.info("请求商店接口，尝试 %s", attempt)
                async with aiohttp.ClientSession(trust_env=True) as session:  # 服务器版唯一改动（R1-对策5）：信任 HTTPS_PROXY
                    async with session.post(
                        self.STORE_URL,
                        headers=headers,
                        json=data,
                        timeout=aiohttp.ClientTimeout(total=timeout),
                    ) as response:
                        response.raise_for_status()
                        response_data = await response.json(content_type=None)

                result_code = response_data.get("result")
                if result_code != 0:
                    err_msg = self._get_store_api_error_message(response_data)

                    # 登录失效，直接返回
                    auth_invalid = self._is_store_auth_invalid(result_code, err_msg)
                    if auth_invalid:
                        return None, err_msg, True

                    # 人数过多，一直等待直到成功
                    if "当前使用人数过多" in err_msg or "人数过多" in err_msg:
                        logger.warning("当前使用人数过多，1分钟后再次重试...")
                        await asyncio.sleep(60)
                        continue

                    # 其它错误正常返回
                    return None, err_msg, False

                return response_data, None, False

            except aiohttp.ClientError as e:
                logger.warning("网络请求失败：%s", e)
                logger.warning("1分钟后重新请求...")
                await asyncio.sleep(60)
                continue

            except Exception as e:
                logger.warning("处理失败：%s", e)
                logger.warning("1分钟后重新请求...")
                await asyncio.sleep(60)
                continue

    async def download_image(self, url: str, filename: str) -> Optional[Path]:
        try:
            TEMP_DIR.mkdir(parents=True, exist_ok=True)
            filepath = TEMP_DIR / filename
            async with aiohttp.ClientSession(trust_env=True) as session:  # 服务器版唯一改动（R1-对策5）：信任 HTTPS_PROXY
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=20)) as response:
                    response.raise_for_status()
                    filepath.write_bytes(await response.read())
            return filepath
        except Exception as e:
            logger.error("下载图片失败 %s：%s", url, e)
            return None

    def _load_pillow(self):
        try:
            from PIL import Image as pil_image, ImageDraw as image_draw, ImageFont as image_font
        except (ImportError, OSError) as exc:
            raise RuntimeError(f"Pillow 不可用：{exc}") from exc
        return pil_image, image_draw, image_font

    def _load_font(self, image_font, size: int = DEFAULT_FONT_SIZE):
        if FONT_FILE.exists():
            try:
                return image_font.truetype(str(FONT_FILE), size)
            except Exception:
                pass

        # Windows 常见中文字体
        for p in [
            "C:/Windows/Fonts/msyh.ttc",
            "C:/Windows/Fonts/simhei.ttf",
            "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        ]:
            try:
                if Path(p).exists():
                    return image_font.truetype(p, size)
            except Exception:
                pass

        logger.warning("未找到中文字体，图片上的中文可能显示异常")
        return image_font.load_default()

    async def generate_shop_image(self, goods_list: List[Dict[str, Any]]) -> Path:
        PILImage, ImageDraw, ImageFont = self._load_pillow()
        processed_images = []
        font = self._load_font(ImageFont, DEFAULT_FONT_SIZE)

        for i, goods in enumerate(goods_list):
            name = str(goods.get("goods_name", f"商品{i+1}"))
            logger.info("处理商品 %s/%s：%s", i + 1, len(goods_list), name)

            bg_img_url = goods.get("bg_image")
            goods_img_url = goods.get("goods_pic")
            if not bg_img_url or not goods_img_url:
                logger.warning("商品缺少图片 URL，跳过：%s", name)
                continue

            bg_path = await self.download_image(bg_img_url, f"bg_{i}.jpg")
            goods_path = await self.download_image(goods_img_url, f"goods_{i}.png")
            if not bg_path or not goods_path:
                continue

            try:
                img1 = PILImage.open(bg_path).convert("RGB")
                img2 = PILImage.open(goods_path).convert("RGBA")

                height = 180
                width = int((img2.width * height) / img2.height)
                img2_resized = img2.resize((width, height))

                x = (img1.width - img2_resized.width) // 2
                y = (img1.height - img2_resized.height) // 2

                new_img = PILImage.new("RGB", img1.size)
                new_img.paste(img1, (0, 0))
                new_img.paste(img2_resized, (x, y), mask=img2_resized)

                draw = ImageDraw.Draw(new_img)

                text_color = (255, 255, 255)
                # 半透明黑底，提高可读性
                overlay = PILImage.new("RGBA", new_img.size, (0, 0, 0, 0))
                odraw = ImageDraw.Draw(overlay)
                odraw.rectangle((0, new_img.height - 74, new_img.width, new_img.height), fill=(0, 0, 0, 110))
                new_img = PILImage.alpha_composite(new_img.convert("RGBA"), overlay).convert("RGB")
                draw = ImageDraw.Draw(new_img)

                price = str(goods.get("rmb_price") or goods.get("price") or "0")
                draw.text((36, new_img.height - 55), name, fill=text_color, font=font)

                price_bbox = draw.textbbox((0, 0), price, font=font)
                price_width = price_bbox[2] - price_bbox[0]
                draw.text((new_img.width - price_width - 36, new_img.height - 55), price, fill=text_color, font=font)

                safe_id = re.sub(r"[^0-9A-Za-z_-]", "_", str(goods.get("goods_id", f"goods_{i+1}"))).strip("_")
                out_path = TEMP_DIR / f"{safe_id or f'goods_{i+1}'}.jpg"
                new_img.save(out_path, quality=92)
                processed_images.append(out_path)

            except Exception as e:
                logger.error("图片处理失败：%s", e)

        if not processed_images:
            raise RuntimeError("没有商品图片处理成功")

        images = [PILImage.open(p).convert("RGB") for p in processed_images]
        max_width = max(img.width for img in images)
        total_height = sum(img.height for img in images) + (len(images) - 1) * 20

        merged_image = PILImage.new("RGB", (max_width, total_height), color="white")
        y_offset = 0
        for img in images:
            merged_image.paste(img, (0, y_offset))
            y_offset += img.height + 20

        merged_path = TEMP_DIR / "merged.jpg"
        merged_image.save(merged_path, quality=92)
        logger.info("商店图片已生成：%s", merged_path)
        return merged_path

    # ---------- 邮件 ----------

    def build_mail_body(self, goods_list: List[Dict[str, Any]]) -> str:
        date = datetime.now().strftime("%Y-%m-%d")
        lines = [f"今日无畏契约商店（{date}）", ""]
        for goods in goods_list:
            name = str(goods.get("goods_name", "未知商品"))
            price = str(goods.get("rmb_price") or goods.get("price") or "0")
            lines.append(f"- {name}：{price}")
        lines.append("")
        lines.append("商品图片见 HTML 邮件正文；支持时也会附带 merged.jpg。")
        return "\n".join(lines)

    def build_mail_html(self, goods_list: List[Dict[str, Any]]) -> str:
        cards = []
        for goods in goods_list:
            name = html.escape(str(goods.get("goods_name", "未知商品")))
            price = html.escape(str(goods.get("rmb_price") or goods.get("price") or "0"))
            image_url = html.escape(str(goods.get("goods_pic") or ""), quote=True)
            background_url = html.escape(str(goods.get("bg_image") or ""), quote=True)
            background_attr = f' background="{background_url}"' if background_url else ""
            background_style = (
                f"background-color:#202733;background-image:url('{background_url}');"
                "background-position:center;background-repeat:no-repeat;background-size:cover;"
                if background_url
                else "background-color:#202733;"
            )
            image_markup = (
                f'<img src="{image_url}" alt="{name}" width="420" height="200" '
                'style="display:block;width:100%;max-width:420px;height:200px;object-fit:contain">'
                if image_url
                else ""
            )
            cards.append(
                '<table role="presentation" width="100%" cellspacing="0" cellpadding="0" '
                'style="width:100%;margin:0 0 20px;border:1px solid #d9dde5;border-collapse:collapse">'
                f'<tr><td{background_attr} height="232" valign="middle" align="center" '
                f'style="{background_style}height:232px;padding:16px">{image_markup}</td></tr>'
                '<tr><td style="padding:14px 16px 16px;background:#fff">'
                f'<div style="font-size:16px"><strong>{name}</strong></div>'
                f'<div style="margin-top:6px;color:#555">价格：{price}</div></td></tr></table>'
            )
        date = datetime.now().strftime("%Y-%m-%d")
        return (
            '<div style="font-family:Arial,\'Microsoft YaHei\',sans-serif;max-width:560px">'
            f'<h2>今日无畏契约商店（{date}）</h2>' + "".join(cards) + "</div>"
        )

    def _deliver_message(self, msg: MIMEMultipart) -> None:
        self.check_mail_config(strict=True)
        recipients = parse_recipients(MAIL_TO or "")
        if not recipients:
            raise RuntimeError("MAIL_TO 未配置有效的收件邮箱")
        smtp_host, smtp_port, smtp_security = smtp_settings(MAIL_USER or "")

        msg["From"] = MAIL_USER
        msg["To"] = ", ".join(recipients)
        logger.info("正在发送邮件到：%s", ", ".join(_mask_email(item) for item in recipients))
        if smtp_security == "ssl":
            with smtplib.SMTP_SSL(smtp_host, smtp_port, timeout=30) as smtp:
                smtp.login(SMTP_USERNAME, MAIL_PASS)
                smtp.sendmail(MAIL_USER, recipients, msg.as_string())
        else:
            with smtplib.SMTP(smtp_host, smtp_port, timeout=30) as smtp:
                if smtp_security == "starttls":
                    smtp.ehlo()
                    smtp.starttls()
                    smtp.ehlo()
                smtp.login(SMTP_USERNAME, MAIL_PASS)
                smtp.sendmail(MAIL_USER, recipients, msg.as_string())

    def send_email(self, goods_list: List[Dict[str, Any]], image_path: Optional[Path] = None) -> None:

        date = datetime.now().strftime("%Y-%m-%d")
        subject = f"{MAIL_SUBJECT_PREFIX}（{date}）"

        msg = MIMEMultipart()
        msg["Subject"] = subject

        msg.attach(MIMEText(self.build_mail_body(goods_list), "plain", "utf-8"))
        msg.attach(MIMEText(self.build_mail_html(goods_list), "html", "utf-8"))

        if image_path and image_path.exists():
            with open(image_path, "rb") as f:
                part = MIMEApplication(f.read(), _subtype="jpg")
                part.add_header("Content-Disposition", "attachment", filename="merged.jpg")
                msg.attach(part)

        self._deliver_message(msg)
        logger.info("邮件发送成功")

    def send_test_email(self) -> None:
        msg = MIMEMultipart()
        msg["Subject"] = f"{MAIL_SUBJECT_PREFIX}（测试）"
        msg.attach(MIMEText("SMTP 配置测试成功。", "plain", "utf-8"))
        self._deliver_message(msg)
        logger.info("测试邮件发送成功")

    def send_auth_expired_notice_once(self) -> None:
        state = AUTH_EXPIRED_STATE_FILE.read_text(encoding="utf-8").strip() if AUTH_EXPIRED_STATE_FILE.exists() else ""
        if state == "sent":
            return
        AUTH_EXPIRED_STATE_FILE.write_text("pending", encoding="utf-8")
        msg = MIMEMultipart()
        msg["Subject"] = f"{MAIL_SUBJECT_PREFIX}（登录凭证已过期）"
        msg.attach(MIMEText("无畏契约登录凭证已过期，请进入路由器管理页面重新扫码绑定账号。", "plain", "utf-8"))
        self._deliver_message(msg)
        AUTH_EXPIRED_STATE_FILE.write_text("sent", encoding="utf-8")
        logger.info("登录凭证过期提醒已发送")

    # ---------- 主流程 ----------

    async def login_flow(self) -> Optional[Dict[str, Any]]:
        print("\n没有检测到 config.json，需要先扫码绑定账号。")
        login_method = os.getenv("VAL_SHOP_LOGIN_METHOD", "").strip().lower()
        if login_method in {"wechat", "2"}:
            choice = "2"
        elif login_method in {"qq", "1"}:
            choice = "1"
        else:
            print("请选择登录方式：")
            print("1. QQ 二维码登录")
            print("2. 微信二维码登录")
            choice = input("输入 1 或 2 后回车：").strip()

        if choice == "2":
            data = await self.wechat_login()
        else:
            data = await self.qq_login()

        if not data or not data.get("userId") or not data.get("tid"):
            logger.error("登录失败，未获取到 userId/tid")
            return None

        self.save_config(data)
        return data

    async def run_once(self) -> None:
        # 没配置邮箱也允许先登录；已有账号后真正发送时会报清晰错误。
        if not CONFIG_FILE.exists() and ALLOW_LOGIN_WITHOUT_MAIL_CONFIG:
            self.check_mail_config(strict=False)
        else:
            self.check_mail_config(strict=True)

        user_config = self.load_config()
        if not user_config:
            if AUTH_EXPIRED_STATE_FILE.exists():
                self.send_auth_expired_notice_once()
                raise RuntimeError("登录凭证已失效，等待重新扫码绑定")
            user_config = await self.login_flow()
            if not user_config:
                raise RuntimeError("账号绑定失败")

        response_data, err_msg, auth_invalid = await self.request_store_api(user_config)
        if not response_data:
            if auth_invalid:
                if not AUTH_EXPIRED_STATE_FILE.exists():
                    AUTH_EXPIRED_STATE_FILE.write_text("pending", encoding="utf-8")
                try:
                    CONFIG_FILE.unlink(missing_ok=True)
                except TypeError:
                    if CONFIG_FILE.exists():
                        CONFIG_FILE.unlink()
                self.send_auth_expired_notice_once()
                raise RuntimeError(f"登录凭证已失效：{err_msg}。已删除 config.json，请重新运行扫码绑定。")
            raise RuntimeError(f"获取商店失败：{err_msg}")

        goods_list, parse_err = self._extract_shop_goods_list(response_data)
        if parse_err:
            raise RuntimeError(parse_err)
        if not goods_list:
            raise RuntimeError("今日商店暂无可用数据")

        image_path = None
        try:
            image_path = await self.generate_shop_image(goods_list)
        except RuntimeError as exc:
            logger.warning("无法生成汇总附件，将发送 HTML 商品图片：%s", exc)
        self.send_email(goods_list, image_path)

        names = [str(g.get("goods_name", "未知商品")) for g in goods_list]
        logger.info("完成。本次商品：%s", " / ".join(names))


async def main():
    parser = argparse.ArgumentParser(description="无畏契约每日商店邮件推送")
    parser.add_argument("--send-test", action="store_true", help="发送 SMTP 测试邮件")
    parser.add_argument("--login-only", action="store_true", help="仅扫码绑定账号")
    args = parser.parse_args()
    app = ValorantShopEmail()
    if args.send_test:
        app.send_test_email()
        return
    if args.login_only:
        try:
            if not await app.login_flow():
                raise RuntimeError("账号绑定失败")
            return
        finally:
            app.cleanup_temp_files()
    try:
        await app.run_once()
    finally:
        app.cleanup_temp_files()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n已取消")
    except Exception as e:
        logger.error("执行失败：%s", e)
        raise SystemExit(1)
