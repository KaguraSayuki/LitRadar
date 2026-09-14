"""Single-user login and locally issued, expiring first-use links."""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
import secrets
import time

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse

from .. import credentials
from ..passwords import MIN_PASSWORD_LENGTH, hash_password, verify_password
from ..settings import SettingsError

router = APIRouter()
SESSION_COOKIE = "litradar_session"
SETUP_COOKIE = "litradar_setup"
SESSION_SECONDS = 12 * 3600
ADMIN_COOKIE = "litradar_admin"
ADMIN_SECONDS = 5 * 60


class PrivateAccessLog(logging.Filter):
    """Uvicorn access logs must not persist setup codes or legacy URL tokens."""
    def filter(self, record):
        if isinstance(record.args, tuple) and len(record.args) == 5:
            args = list(record.args)
            args[2] = str(args[2]).split('?', 1)[0]
            record.args = tuple(args)
        return True


_access_filter = PrivateAccessLog()


def protect_access_log():
    # Install after the server's logging configuration, before its first access log.
    logging.getLogger('uvicorn.access').addFilter(_access_filter)


def session_value(cfg, *, expires=None) -> str:
    expiry = str(expires or int(time.time()) + SESSION_SECONDS)
    nonce = secrets.token_hex(16)
    body = expiry + "." + nonce
    signature = hmac.new((cfg.app.admin_password_hash or "").encode(),
                         ("litradar-session:" + body).encode(), hashlib.sha256).hexdigest()
    return body + "." + signature


def logged_in(request: Request, cfg) -> bool:
    if not cfg.app.admin_password_hash:
        return False
    try:
        expiry, nonce, signature = request.cookies.get(SESSION_COOKIE, "").split(".")
        if int(expiry) <= time.time() or int(expiry) > time.time() + SESSION_SECONDS + 30:
            return False
        expected = hmac.new(cfg.app.admin_password_hash.encode(),
            ("litradar-session:" + expiry + "." + nonce).encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(signature, expected)
    except (ValueError, TypeError):
        return False


def set_session(response, request, cfg, *, admin_until=None):
    response.set_cookie(SESSION_COOKIE, session_value(cfg), httponly=True,
                        secure=request.url.scheme == "https", samesite="strict",
                        max_age=SESSION_SECONDS)
    set_admin_session(response, request, cfg, expires=admin_until)


def admin_value(cfg, *, expires=None) -> str:
    expiry = str(expires if expires is not None else int(time.time()) + ADMIN_SECONDS)
    body = expiry + "." + secrets.token_hex(16)
    signature = hmac.new((cfg.app.admin_password_hash or "").encode(),
                         ("litradar-admin:" + body).encode(), hashlib.sha256).hexdigest()
    return body + "." + signature


def admin_expires(request: Request, cfg) -> int:
    if not cfg.app.admin_password_hash:
        return 0
    try:
        expiry, nonce, signature = request.cookies.get(ADMIN_COOKIE, "").split(".")
        timestamp = int(expiry)
        if not time.time() < timestamp <= time.time() + ADMIN_SECONDS + 1:
            return 0
        expected = hmac.new(cfg.app.admin_password_hash.encode(),
            ("litradar-admin:" + expiry + "." + nonce).encode(), hashlib.sha256).hexdigest()
        return timestamp if hmac.compare_digest(signature, expected) else 0
    except (ValueError, TypeError):
        return 0


def set_admin_session(response, request, cfg, *, expires=None):
    response.set_cookie(ADMIN_COOKIE, admin_value(cfg, expires=expires), httponly=True,
                        secure=request.url.scheme == "https", samesite="strict", max_age=ADMIN_SECONDS)


def check_password(cfg, password: str) -> str:
    """Share the persisted attempt limit between login and short operation grants."""
    def attempt(data):
        state = data.setdefault("login_attempts", {"start": time.time(), "failures": 0})
        if time.time() - state["start"] >= 300:
            state.update(start=time.time(), failures=0)
        if state["failures"] >= 10:
            return "尝试次数过多，请五分钟后重试。"
        if len(password) > 512 or not verify_password(password, cfg.app.admin_password_hash or ""):
            state["failures"] += 1
            return "密码不正确，请重新输入。"
        data.pop("login_attempts", None)
        return ""
    return credentials.mutate(cfg, attempt)


def locked_page(request):
    from . import app as web
    return web.templates.TemplateResponse(request, "settings/locked.html",
        web.ctx(request, page="settings", access_reload=True))


def safe_next(value: str) -> str:
    from urllib.parse import urlsplit
    if (not value.startswith('/') or value.startswith('//') or '\\' in value
            or any(ord(c) < 32 for c in value)):
        return '/settings'
    parsed = urlsplit(value)
    if parsed.scheme or parsed.netloc:
        return '/settings'
    return value if parsed.path.startswith(('/settings', '/stats', '/interests')) else '/settings'


@router.post("/access/unlock")
async def unlock(request: Request):
    from . import app as web
    from fastapi.responses import JSONResponse
    web.require_same_origin(request)
    cfg = web.get_cfg()
    form = await request.form()
    error = check_password(cfg, str(form.get("password", "")))
    if error:
        if 'application/json' not in request.headers.get('accept', ''):
            return page(request, 'login', error=error, ready=True, status=401)
        raise SettingsError(error, status=401)
    expiry = int(time.time()) + ADMIN_SECONDS
    response = (JSONResponse({"expires_at": expiry, "server_time": time.time()})
        if 'application/json' in request.headers.get('accept', '')
        else RedirectResponse(safe_next(str(form.get('next', '/settings'))), status_code=303))
    set_session(response, request, cfg, admin_until=expiry)
    return response


@router.get("/access/status")
def authorization_status(request: Request):
    from . import app as web
    web.require_token(request)
    cfg = web.get_cfg()
    return {"expires_at": admin_expires(request, cfg), "server_time": time.time(),
            "required": bool(cfg.app.admin_password_hash)}


@router.post("/access/lock")
async def lock_operations(request: Request):
    from . import app as web
    from fastapi.responses import JSONResponse
    web.require_same_origin(request)
    cfg = web.get_cfg()
    if 'application/json' in request.headers.get('accept', ''):
        response = JSONResponse({"expires_at": 0, "server_time": time.time(),
                                 "required": bool(cfg.app.admin_password_hash)})
    else:
        form = await request.form()
        response = RedirectResponse(safe_next(str(form.get('next', '/settings'))), status_code=303)
    # Clear only this browser's operation grant; reading and other clients continue.
    response.delete_cookie(ADMIN_COOKIE, secure=request.url.scheme == 'https', samesite='strict', httponly=True)
    return response


def issue_setup_link(cfg, url: str) -> str:
    from urllib.parse import urlsplit
    parsed = urlsplit(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.query or parsed.fragment:
        raise ValueError("请填写实例的完整 HTTP 或 HTTPS 地址。")
    if cfg.app.admin_password_hash:
        raise ValueError("已经设置访问密码。请登录网页修改密码，或由维护代理恢复访问。")
    code = secrets.token_urlsafe(32)
    def update(data):
        if data.get('values', {}).get(cfg.app.admin_password_env):
            raise ValueError("已经设置访问密码，请登录网页。")
        data["setup"] = {"digest": hashlib.sha256(code.encode()).hexdigest(),
                         "expires": int(time.time()) + 1800}
    credentials.mutate(cfg, update)
    return url.rstrip("/") + "/setup?code=" + code


def valid_setup(data: dict, code: str) -> bool:
    setup = data.get("setup") or {}
    return bool(code and setup.get("expires", 0) > time.time() and hmac.compare_digest(
        setup.get("digest", ""), hashlib.sha256(code.encode()).hexdigest()))


def page(request, mode, *, error="", status=200, ready=False):
    from . import app as web
    response = web.templates.TemplateResponse(request, "settings/access.html",
        web.ctx(request, page="settings", mode=mode, error=error, ready=ready), status_code=status)
    response.headers.update({"Cache-Control": "no-store", "Referrer-Policy": "same-origin"})
    return response


@router.get("/setup")
def setup_page(request: Request, code: str = ""):
    from . import app as web
    cfg = web.get_cfg()
    if cfg.app.admin_password_hash:
        return RedirectResponse("/settings" if logged_in(request, cfg) else "/login", status_code=303)
    if code:
        if not valid_setup(credentials.read_store(cfg), code):
            return page(request, "setup", error="设置链接已失效，请让启动实例的代理重新生成。", status=403)
        response = RedirectResponse("/setup", status_code=303)
        response.set_cookie(SETUP_COOKIE, code, httponly=True, samesite="strict", max_age=1800,
                            secure=request.url.scheme == "https", path="/setup")
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        return response
    ready = valid_setup(credentials.read_store(cfg), request.cookies.get(SETUP_COOKIE, ""))
    return page(request, "setup", ready=ready)


@router.post("/setup")
async def setup_save(request: Request):
    from . import app as web
    web.require_same_origin(request)
    cfg = web.get_cfg()
    if cfg.app.admin_password_hash:
        return page(request, 'login', error='这个实例已完成密码设置，请登录。', status=409, ready=True)
    if not valid_setup(credentials.read_store(cfg), request.cookies.get(SETUP_COOKIE, '')):
        return page(request, 'setup', error='设置链接已失效，请让启动实例的代理重新生成。', status=403)
    form = await request.form()
    password = str(form.get("password", ""))
    if len(password) < MIN_PASSWORD_LENGTH or len(password) > 512:
        return page(request, "setup", error="密码请使用 8 至 512 个字符。", status=400, ready=True)
    if password != form.get("confirm"):
        return page(request, "setup", error="两次密码不一致，请重新输入。", status=400, ready=True)
    hashed = hash_password(password)
    def update(data):
        if cfg.app.admin_password_hash or data.get("values", {}).get(cfg.app.admin_password_env):
            raise SettingsError("这个实例已完成密码设置，请登录。", status=409)
        if cfg.app.admin_password_env in os.environ:
            raise SettingsError("访问密码由部署环境管理，请让维护代理更换。", status=409)
        if not valid_setup(data, request.cookies.get(SETUP_COOKIE, "")):
            raise SettingsError("设置链接已失效，请让启动实例的代理重新生成。", status=403)
        data.setdefault("values", {})[cfg.app.admin_password_env] = hashed
        data.pop("setup", None)
        data["setup_complete"] = True
    credentials.mutate(cfg, update)
    cfg = web.get_cfg()
    response = RedirectResponse("/settings/services?welcome=1", status_code=303)
    response.delete_cookie(SETUP_COOKIE, path="/setup")
    set_session(response, request, cfg)
    return response


@router.get("/login")
def login_page(request: Request):
    return page(request, "login", ready=True)


@router.post("/login")
async def login(request: Request):
    from . import app as web
    web.require_same_origin(request)
    cfg = web.get_cfg()
    form = await request.form()
    password = str(form.get("password", ""))
    error = check_password(cfg, password)
    if error:
        return page(request, "login", error=error, ready=True, status=401)
    response = RedirectResponse("/settings", status_code=303)
    set_session(response, request, cfg)
    return response


@router.post("/logout")
def logout(request: Request):
    from . import app as web
    web.require_same_origin(request)
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(SESSION_COOKIE)
    response.delete_cookie(ADMIN_COOKIE)
    response.delete_cookie(web.COOKIE_NAME)
    return response
