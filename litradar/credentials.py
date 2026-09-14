"""Credential provenance, private writes, and per-operation snapshots."""
from __future__ import annotations

import json
import os
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

from . import config

_active: ContextVar[dict | None] = ContextVar("litradar_credentials", default=None)


def store_path(cfg=None) -> Path:
    path = getattr(cfg, "config_file", None)
    path = path or config._expand(os.environ.get("LITRADAR_CONFIG", "config.yaml"))
    return Path(path).parent / ".litradar-secrets.json"


def read_store(cfg=None) -> dict:
    path = store_path(cfg)
    if not path.exists():
        return {}
    # Invalid private files must fail closed, especially for access protection.
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("values", {}), dict):
        raise ValueError("无法读取凭据文件，请恢复实例的私有备份。")
    return data


def resolve(cfg=None) -> dict:
    path = store_path(cfg).parent / ".env"
    config.load_env_file(path)
    return {**config._file_values, **read_store(cfg).get("values", {}), **os.environ}


def current_values() -> dict:
    active = _active.get()
    return active if active is not None else resolve()


def attach_snapshot(cfg) -> None:
    values = resolve(cfg)
    cfg._secret_values = values
    for section in (cfg.app, cfg.mail, cfg.llm):
        section._secret_values = values


@contextmanager
def snapshot(cfg):
    values = getattr(cfg, "_secret_values", None)
    token = _active.set(values if values is not None else resolve(cfg))
    try:
        yield
    finally:
        _active.reset(token)


def services(cfg) -> dict[str, tuple[str, str]]:
    return {
        "s2": ("Semantic Scholar", "S2_API_KEY"),
        "llm": ("AI 模型服务", cfg.llm.api_key_env),
        "openalex": ("OpenAlex", cfg.sources.openalex_api_key_env),
        "journal": ("easyScholar 期刊标签", cfg.journal_rank.api_key_env),
        "mail": ("邮箱授权码", cfg.mail.imap_password_env),
    }


def status(cfg, name: str) -> dict:
    values = resolve(cfg)
    return {"configured": bool((values.get(name) or "").strip()),
            "managed": name not in os.environ,
            "source": "由部署环境管理" if name in os.environ else "可在网页修改"}


def mutate(cfg, operation):
    from .lock import AlreadyRunning, single_instance
    from .settings import SettingsError, atomic_write

    path = store_path(cfg)
    try:
        with single_instance(path.with_name(path.name + ".lock")):
            data = read_store(cfg)
            result = operation(data)
            atomic_write(path, json.dumps(data, ensure_ascii=False).encode(), backup=False)
            return result
    except AlreadyRunning:
        raise SettingsError("另一处正在保存凭据，请稍后重试。原设置已保留。", status=409) from None


def write(cfg, name: str, value: str | None, *, expected: str | None = None) -> None:
    from .settings import SettingsError, revision

    if name in os.environ:
        raise SettingsError("这个凭据由部署环境管理。请让维护实例的代理更换部署凭据后重启。")
    if value is not None and (len(value) > 8192 or "\n" in value or "\r" in value):
        raise SettingsError("凭据过长或包含换行，请重新粘贴完整的一行。")
    def update(data):
        if expected is not None and expected != revision(store_path(cfg)):
            raise SettingsError("凭据已在另一页面修改，请重新打开此页面。", status=409)
        # Null masks legacy .env values; clearing cannot resurrect an old key.
        data.setdefault("values", {})[name] = value.strip() if value else None
    mutate(cfg, update)
