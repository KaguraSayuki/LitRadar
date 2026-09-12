"""Microsoft Outlook IMAP OAuth2 authentication.

The :mod:`msal` dependency is deliberately optional.  Password based IMAP
continues to work without it; callers only need to install the optional MSAL
extra when ``mail.imap_auth`` is ``oauth2``.

This module never prints or logs access tokens.  Device-code text is returned
to the caller through ``on_device_code`` so a CLI can show Microsoft's normal
user instructions during an explicit first-time authorization.  Automated
mail ingestion calls :func:`authenticate` without ``interactive=True`` and
therefore only uses the local serialized MSAL cache and its refresh token.
"""
from __future__ import annotations

import importlib
import json
import os
import re
import secrets
import stat
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from ..config import ROOT

IMAP_SCOPE = "https://outlook.office.com/IMAP.AccessAsUser.All"
SCOPES = (IMAP_SCOPE,)
AUTHORITY_HOST = "https://login.microsoftonline.com"
DEFAULT_CLIENT_ID_ENV = "OUTLOOK_CLIENT_ID"
DEFAULT_TENANT = "consumers"
DEFAULT_TOKEN_CACHE = "./data/outlook-token-cache.json"
OAUTH_IMAP_HOST = "outlook.office365.com"
MSAL_TIMEOUT = 30

_TENANT_NAME_RE = re.compile(r"^(?:common|consumers|organizations)$", re.I)
_TENANT_ID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.I,
)
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


class OAuthError(RuntimeError):
    """Base class for errors that should be shown as an IMAP auth failure."""


class OAuthConfigurationError(OAuthError):
    """The local OAuth configuration is incomplete or unsafe."""


class OAuthDependencyError(OAuthError):
    """MSAL is not installed although OAuth mode was requested."""


class OAuthAuthorizationRequired(OAuthError):
    """The cache cannot silently produce a usable access token."""


class OAuthAuthenticationError(OAuthError):
    """The IMAP server rejected a validly-shaped OAuth attempt."""


def _value(cfg: Any, name: str, default: Any) -> Any:
    """Read a new optional MailConfig field without breaking old callers."""

    value = getattr(cfg, name, default)
    return default if value is None else value


def _client_id(cfg: Any) -> str:
    env_name = str(_value(cfg, "oauth_client_id_env", DEFAULT_CLIENT_ID_ENV)).strip()
    if not env_name:
        raise OAuthConfigurationError("mail.oauth_client_id_env 不能为空")
    client_id = os.environ.get(env_name, "").strip()
    if not client_id:
        raise OAuthConfigurationError(f"环境变量 {env_name} 未设置(Outlook OAuth client ID)")
    return client_id


def _authority(cfg: Any) -> str:
    """Build an authority on Microsoft's fixed host.

    Tenant may be one of the standard endpoint names or a GUID.  Arbitrary
    URLs are rejected so a config typo cannot redirect token acquisition to a
    non-Microsoft authority.
    """

    tenant = str(_value(cfg, "oauth_tenant", DEFAULT_TENANT)).strip()
    if not (_TENANT_NAME_RE.fullmatch(tenant) or _TENANT_ID_RE.fullmatch(tenant)):
        raise OAuthConfigurationError(
            "mail.oauth_tenant 必须是 consumers/common/organizations 或租户 GUID"
        )
    return f"{AUTHORITY_HOST}/{tenant}"


def cache_path(cfg: Any) -> Path:
    """Return the configured cache path, rooted at the project for relatives."""

    raw = str(_value(cfg, "oauth_token_cache", DEFAULT_TOKEN_CACHE)).strip()
    if not raw:
        raise OAuthConfigurationError("mail.oauth_token_cache 不能为空")
    path = Path(raw).expanduser()
    resolved = path if path.is_absolute() else ROOT / path
    # 缓存里是明文的 access/refresh token。放在仓库内就必须落在 data/ 下:
    # .gitignore 只忽略 data/,若配成 ./outlook-token-cache.json 这种项目根
    # 路径,文件会被 git 追踪并随 push 推到远端。仓库外的绝对路径不受影响。
    try:
        path_resolved = resolved.resolve()
        root_resolved = Path(ROOT).resolve()
    except OSError as exc:
        raise OAuthConfigurationError("mail.oauth_token_cache 路径无法解析") from exc
    if root_resolved in path_resolved.parents \
            and (root_resolved / "data") not in path_resolved.parents:
        raise OAuthConfigurationError(
            "mail.oauth_token_cache 在项目内必须放在 data/ 下,否则会被 git 追踪")
    return resolved


def validate_imap_host(host: Any) -> None:
    """Only send Outlook bearer tokens to Microsoft's documented IMAP host."""

    normalized = str(host or "").strip().rstrip(".").casefold()
    if normalized != OAUTH_IMAP_HOST:
        raise OAuthConfigurationError(
            f"Outlook OAuth 只允许连接 {OAUTH_IMAP_HOST}，当前为 {host!r}"
        )


def _msal() -> Any:
    try:
        return importlib.import_module("msal")
    except ImportError as exc:  # pragma: no cover - exercised with a fake module
        raise OAuthDependencyError(
            "Outlook OAuth 需要安装可选依赖 msal(例如 pip install 'litradar[outlook]')"
        ) from exc


def _cache_from_file(
    msal_module: Any,
    path: Path,
    *,
    allow_reinitialize: bool = False,
) -> Any:
    cache = msal_module.SerializableTokenCache()
    if not path.exists():
        return cache
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
        if mode != 0o600:
            os.chmod(path, 0o600)
    except OSError as exc:
        raise OAuthAuthorizationRequired(
            "Outlook OAuth 缓存权限无法收紧到 0600，请检查文件权限"
        ) from exc
    try:
        # Reading is intentionally explicit rather than using pickle or an
        # opaque cache helper: the file is local JSON and can be backed up.
        cache.deserialize(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        if allow_reinitialize:
            # Keep the old file in place until device authorization succeeds;
            # _write_cache() replaces it atomically only after a new token is
            # available.
            return msal_module.SerializableTokenCache()
        raise OAuthAuthorizationRequired(
            "Outlook OAuth 缓存无法读取，请运行设备码授权重新建立缓存"
        ) from exc
    return cache


def _write_cache(cache: Any, path: Path) -> None:
    """Persist an MSAL cache with mode 0600 and an atomic replace."""

    try:
        serialized = cache.serialize()
    except Exception as exc:  # noqa: BLE001 - redact provider internals
        raise OAuthError("Outlook OAuth 缓存序列化失败") from exc
    if not isinstance(serialized, str):
        raise OAuthError("Outlook OAuth 缓存格式无效")

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(8)}.tmp")
    fd: int | None = None
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fd = None
            fh.write(serialized)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
        # Best effort: fsync the directory where supported, so a successful
        # replace is not lost on a power failure.
        try:
            dir_fd = os.open(path.parent, os.O_RDONLY)
        except OSError:
            dir_fd = None
        if dir_fd is not None:
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
    except OSError as exc:
        raise OAuthError("Outlook OAuth 缓存写入失败") from exc
    finally:
        if fd is not None:
            os.close(fd)
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass


def _save_if_changed(cache: Any, path: Path) -> None:
    if bool(getattr(cache, "has_state_changed", False)):
        _write_cache(cache, path)


def _account_for(
    app: Any,
    cfg: Any,
    *,
    allow_reauthorize: bool = False,
) -> Mapping[str, Any] | None:
    accounts = list(app.get_accounts() or [])
    if not accounts:
        return None
    wanted = str(getattr(cfg, "imap_user", "") or "").strip().casefold()
    if wanted:
        matching = [
            account for account in accounts
            if str(account.get("username", "")).strip().casefold() == wanted
        ]
        if len(matching) == 1:
            return matching[0]
        if allow_reauthorize:
            return None
        raise OAuthAuthorizationRequired(
            "Outlook OAuth 缓存账户与目标邮箱不匹配，请运行设备码授权"
        )
    if len(accounts) == 1:
        return accounts[0]
    if allow_reauthorize:
        return None
    raise OAuthAuthorizationRequired(
        "Outlook OAuth 缓存包含多个账户，请运行设备码授权并使用目标邮箱"
    )


def _token_result(result: Any, *, interactive: bool) -> str | None:
    if isinstance(result, Mapping):
        token = result.get("access_token")
        if isinstance(token, str) and token:
            return token
    # Do not include provider result/error_description: some providers echo
    # request material, while the useful user action is always the same.
    if interactive:
        raise OAuthAuthorizationRequired("Outlook OAuth 设备码授权未返回有效访问令牌")
    return None


def acquire_token(
    cfg: Any,
    *,
    interactive: bool = False,
    on_device_code: Callable[[Mapping[str, Any]], None] | None = None,
) -> str:
    """Get an IMAP access token from cache, or explicitly run device flow.

    Automated ingestion leaves ``interactive`` false.  In that mode a missing
    or expired cache raises :class:`OAuthAuthorizationRequired` instead of
    blocking a scheduler waiting for user input.  The CLI's explicit OAuth
    setup command can pass ``interactive=True`` and display the returned flow
    information through ``on_device_code``.
    """

    msal_module = _msal()
    path = cache_path(cfg)
    cache = _cache_from_file(
        msal_module,
        path,
        allow_reinitialize=interactive,
    )
    app = msal_module.PublicClientApplication(
        _client_id(cfg),
        authority=_authority(cfg),
        token_cache=cache,
        timeout=MSAL_TIMEOUT,
    )
    account = _account_for(app, cfg, allow_reauthorize=interactive)
    if account is not None:
        try:
            result = app.acquire_token_silent(list(SCOPES), account=account)
        except Exception as exc:  # noqa: BLE001 - do not expose token/provider data
            result = None
            silent_error = exc
        else:
            silent_error = None
        token = _token_result(result, interactive=False)
        _save_if_changed(cache, path)
        if token:
            return token
        if silent_error is not None and not interactive:
            raise OAuthAuthorizationRequired(
                "Outlook OAuth 缓存刷新失败，请运行设备码授权重新授权"
            ) from silent_error

    if not interactive:
        raise OAuthAuthorizationRequired(
            "未找到可静默刷新的 Outlook OAuth 令牌，请运行设备码授权"
        )

    try:
        flow = app.initiate_device_flow(scopes=list(SCOPES))
    except Exception as exc:  # noqa: BLE001
        raise OAuthAuthorizationRequired("无法启动 Outlook OAuth 设备码授权") from exc
    if not isinstance(flow, Mapping) or not flow.get("user_code"):
        raise OAuthAuthorizationRequired("Outlook OAuth 设备码响应无效")
    if on_device_code is not None:
        on_device_code(flow)
    try:
        result = app.acquire_token_by_device_flow(flow)
    except Exception as exc:  # noqa: BLE001
        raise OAuthAuthorizationRequired("Outlook OAuth 设备码授权失败") from exc
    token = _token_result(result, interactive=True)
    assert token is not None  # _token_result raises otherwise
    _save_if_changed(cache, path)
    return token


def device_authorize(
    cfg: Any,
    *,
    on_device_code: Callable[[Mapping[str, Any]], None] | None = None,
) -> str:
    """Explicit first-time authorization entry point for a CLI command."""

    return acquire_token(cfg, interactive=True, on_device_code=on_device_code)


def authenticate(
    conn: Any,
    cfg: Any,
    *,
    interactive: bool = False,
    on_device_code: Callable[[Mapping[str, Any]], None] | None = None,
) -> tuple[Any, Any]:
    """Authenticate an existing IMAP connection with SASL XOAUTH2.

    The callback passed to ``imaplib.authenticate`` returns the raw XOAUTH2
    bytes; ``imaplib`` performs the required base64 encoding.  The return value
    is the server's ``(typ, data)`` pair, and anything other than ``OK`` raises
    so callers cannot mistake a rejected login for a successful one.
    """

    validate_imap_host(getattr(cfg, "imap_host", ""))
    raw_user = str(getattr(cfg, "imap_user", "") or "")
    if _CONTROL_RE.search(raw_user):
        raise OAuthConfigurationError("mail.imap_user 不能包含控制字符")
    user = raw_user.strip()
    if not user:
        raise OAuthConfigurationError("mail.imap_user 不能为空")
    token = acquire_token(
        cfg,
        interactive=interactive,
        on_device_code=on_device_code,
    )
    payload = f"user={user}\x01auth=Bearer {token}\x01\x01".encode("utf-8")
    first_challenge = True

    def _response(challenge: Any) -> bytes:
        nonlocal first_challenge
        # A successful XOAUTH2 exchange sends the payload once in response to
        # the initial empty challenge.  Never echo a token to an error or a
        # repeated challenge from the server.
        if not first_challenge:
            return b""
        first_challenge = False
        if challenge not in (b"", ""):
            return b""
        return payload

    try:
        result = conn.authenticate("XOAUTH2", _response)
    except Exception as exc:  # noqa: BLE001
        raise OAuthAuthenticationError("Outlook IMAP XOAUTH2 认证失败") from exc
    if (
        not isinstance(result, tuple)
        or not result
        or str(result[0]).upper() != "OK"
    ):
        raise OAuthAuthenticationError("Outlook IMAP XOAUTH2 认证被服务器拒绝")
    return result


__all__ = [
    "AUTHORITY_HOST",
    "DEFAULT_CLIENT_ID_ENV",
    "DEFAULT_TENANT",
    "DEFAULT_TOKEN_CACHE",
    "IMAP_SCOPE",
    "MSAL_TIMEOUT",
    "OAUTH_IMAP_HOST",
    "OAuthAuthenticationError",
    "OAuthAuthorizationRequired",
    "OAuthConfigurationError",
    "OAuthDependencyError",
    "OAuthError",
    "SCOPES",
    "acquire_token",
    "authenticate",
    "cache_path",
    "device_authorize",
    "validate_imap_host",
]
