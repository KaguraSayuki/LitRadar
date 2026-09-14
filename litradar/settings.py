"""Versioned, file-backed settings shared by the web UI and local commands."""
from __future__ import annotations

import copy
import hashlib
import os
import tempfile
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from .config import Config, _expand, config_from_dict
from .lock import AlreadyRunning, single_instance
from .normalize import find_doi
from .rank import load_groups, validate_interests

MAX_BYTES = 512 * 1024
BACKUPS = 5


class SettingsError(ValueError):
    def __init__(self, message: str, field: str = "", status: int = 400):
        super().__init__(message)
        self.field, self.status = field, status


def revision(path: Path) -> str:
    return hashlib.sha256(path.read_bytes() if path.exists() else b"").hexdigest()


def read_mapping(path: Path) -> dict:
    if not path.exists():
        return {}
    if path.stat().st_size > MAX_BYTES:
        raise SettingsError("设置文件过大，请让维护代理恢复本机备份。")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (yaml.YAMLError, UnicodeError):
        raise SettingsError("无法读取现有设置，请让维护代理恢复本机备份。") from None
    if not isinstance(data, dict):
        raise SettingsError("现有设置格式不正确，请让维护代理恢复本机备份。")
    return data


def atomic_write(path: Path, content: bytes, *, backup: bool = True) -> None:
    try:
        _atomic_write(path, content, backup=backup)
    except OSError:
        raise SettingsError("无法保存文件，原设置已保留。请让维护代理检查磁盘空间和文件权限后重试。", status=503) from None


def _atomic_write(path: Path, content: bytes, *, backup: bool = True) -> None:
    """The caller holds the file's settings lock. No shared temporary names."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if backup and path.exists():
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
        saved = path.with_name(f"{path.name}.{stamp}-{uuid.uuid4().hex[:6]}.bak")
        atomic_write(saved, path.read_bytes(), backup=False)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)
    if backup:
        for old in sorted(path.parent.glob(f"{path.name}.*.bak"))[:-BACKUPS]:
            try:
                old.unlink(missing_ok=True)
            except OSError:
                pass  # the new settings are committed; pruning can retry later


def dump(data: dict) -> bytes:
    raw = yaml.safe_dump(data, allow_unicode=True, sort_keys=False).encode("utf-8")
    if len(raw) > MAX_BYTES:
        raise SettingsError("设置内容过多，请减少条目后再保存。")
    return raw


def get_value(data: dict, dotted: str, default=None):
    value = data
    for key in dotted.split("."):
        if not isinstance(value, dict) or key not in value:
            return default
        value = value[key]
    return value


def put_value(data: dict, dotted: str, value: Any) -> None:
    parts = dotted.split(".")
    for key in parts[:-1]:
        if not isinstance(data.get(key), dict):
            data[key] = {}
        data = data[key]
    data[parts[-1]] = value


def lines(value: str) -> list[str]:
    return list(dict.fromkeys(s.strip() for s in value.splitlines() if s.strip()))


GROUP_FIELDS = {
    "keywords.core": ("核心关键词", "最能代表研究方向的词语，例如 photocatalysis"),
    "keywords.bonus": ("加分关键词", "出现这些词语时优先推荐"),
    "keywords.current_challenges": ("当前研究难点", "希望从文献中解决的问题"),
    "keywords.boost_topics": ("近期关注主题", "可填写完整句子"),
    "negative": ("排除内容", "标题或摘要包含这些词语时排除"),
    "negative_titles": ("排除标题中的词语", "只检查标题，避免误伤摘要里的背景介绍"),
    "exclude_title_prefixes": ("排除以这些文字开头的标题", "例如 Correction、Editorial"),
    "journals.core": ("重点关注的期刊", "填写期刊全名或常见缩写"),
    "journals.ok": ("其他关注期刊", "辅助排序，不会限制其他期刊的检索结果"),
    "authors_watch": ("关注的作者", "填写论文中的作者姓名"),
    "seed_dois": ("追踪这些论文的后续引用", "粘贴 DOI 或包含 DOI 的论文链接"),
}


@dataclass
class Snapshot:
    config: dict
    interests: dict
    version: str


class SettingsStore:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.path = cfg.config_file or _expand(os.environ.get("LITRADAR_CONFIG", "config.yaml"))
        self.interests_path = cfg.interests_file

    @contextmanager
    def locked(self):
        try:
            with single_instance(self.path.with_name(self.path.name + ".settings.lock")):
                yield
        except AlreadyRunning:
            raise SettingsError("另一处正在保存设置，请稍后再试。原设置已保留。", status=409) from None

    def version(self) -> str:
        return hashlib.sha256((str(self.interests_path) + revision(self.path)
                               + revision(self.interests_path)).encode()).hexdigest()

    def snapshot(self) -> Snapshot:
        with self.locked():
            before = self.version()
            config = read_mapping(self.path)
            interests = read_mapping(self.interests_path)
            if self.cfg.config_file and config_from_dict(config).interests_file != self.interests_path:
                raise SettingsError("研究方向文件位置已变化，请重新载入。", status=409)
            if before != self.version():
                raise SettingsError("设置正在被本机程序修改，请重新载入。", status=409)
            return Snapshot(config, interests, before)

    def check(self, expected: str) -> None:
        if not expected or expected != self.version():
            raise SettingsError("设置已在另一页面或本机修改。您的输入仍保留，请重新打开设置页对照后保存。",
                                status=409)

    def update_config(self, patch: dict[str, Any], expected: str) -> None:
        with self.locked():
            self.check(expected)
            data = read_mapping(self.path)
            for key, value in patch.items():
                put_value(data, key, value)
                if key.startswith("ranking.weights."):
                    data.get("ranking", {}).pop("w_" + key.rsplit(".", 1)[1], None)
            config_from_dict(data)  # reject before making a backup
            atomic_write(self.path, dump(data))

    def group_entries(self, data: dict) -> list[dict]:
        if not data:
            return []
        cfg = copy.copy(self.cfg)
        cfg.interests_data = data
        profiles = load_groups(cfg)
        entries = copy.deepcopy(data.get("groups") if data.get("groups") is not None else [data])
        for entry, profile in zip(entries, profiles):
            entry["slug"] = profile.slug
            entry.setdefault("name", profile.name)
        return entries

    def update_group(self, slug: str | None, values: dict, expected: str,
                     *, action: str = "save") -> str:
        with self.locked():
            self.check(expected)
            data = read_mapping(self.interests_path)
            entries = self.group_entries(data)
            entry = next((g for g in entries if g["slug"] == slug), None)
            if slug and entry is None:
                raise SettingsError("这个研究方向已变化，请重新打开研究方向列表。", status=409)
            if action == "copy":
                entry = copy.deepcopy(entry)
                entry["name"] += "（副本）"
                entry["slug"] = "g-" + uuid.uuid4().hex[:12]
                entries.append(entry)
            elif action in ("disable", "enable"):
                entry["enabled"] = action == "enable"
            elif action in ("up", "down"):
                index = entries.index(entry)
                other = index + (-1 if action == "up" else 1)
                if 0 <= other < len(entries):
                    entries[index], entries[other] = entries[other], entries[index]
            elif action == "save":
                if entry is None:
                    entry = {"slug": "default" if not entries and not data else "g-" + uuid.uuid4().hex[:12]}
                    entries.append(entry)
                for key, value in values.items():
                    put_value(entry, key, value)
            else:
                raise SettingsError("不支持的方向操作。")
            # A legacy flat file becomes the default group, including its extension fields.
            root = copy.deepcopy(data) if data.get("groups") is not None else {}
            root["groups"] = entries
            errors = validate_interests(root)
            if errors:
                raise SettingsError("研究方向内容不完整，请检查名称和列表输入。")
            atomic_write(self.interests_path, dump(root))
            return entry["slug"]

    def backups(self, kind: str) -> list[Path]:
        target = self.path if kind == 'config' else self.interests_path
        return list(reversed(sorted(target.parent.glob(target.name + '.*.bak'))))[:BACKUPS]

    def restore(self, kind: str, name: str, expected: str, *, preview=False) -> dict:
        if kind not in ('config', 'interests'):
            raise SettingsError("请选择配置备份或研究方向备份。")
        with self.locked():
            self.check(expected)
            path = next((p for p in self.backups(kind) if p.name == name), None)
            if path is None:
                raise SettingsError("备份已变化，请重新打开维护页面。", status=409)
            data = read_mapping(path)
            if kind == 'interests':
                if validate_interests(data):
                    raise SettingsError("这份备份的研究方向格式不完整，请选择另一份备份。")
                detail = {'directions': [g.get('name',g['slug']) for g in self.group_entries(data)]}
                target = self.interests_path
            else:
                # A preference restore must not change deployment, authentication,
                # database paths, or silently reactivate automatic execution.
                current = config_from_dict(read_mapping(self.path))
                for field in ('host','port','db_path','interests','token_env','admin_password_env'):
                    put_value(data, 'app.' + field, getattr(current.app,field))
                for section in ('llm','mail','sources','journal_rank'):
                    for key,value in vars(getattr(current,section)).items():
                        if key.endswith('_env'):
                            put_value(data, section + '.' + key, value)
                put_value(data, 'schedule.enabled', False)
                put_value(data, 'schedule.owner', current.schedule.owner)
                put_value(data, 'schedule.handoff_confirmed', current.schedule.handoff_confirmed)
                from .settings_fields import validate_restored_config
                restored = config_from_dict(data)
                restored.interests_data = read_mapping(self.interests_path)
                validate_restored_config(restored)
                detail = {'sections': [k for k in ('sources','mail','llm','ranking','journal_rank','admin','schedule') if k in data]}
                target = self.path
            if not preview:
                atomic_write(target, dump(data))
            return detail


def group_patch(form) -> dict:
    name = str(form.get("name", "")).strip()
    if not name or len(name) > 100:
        raise SettingsError("请填写研究方向名称，最多 100 个字。", "name")
    result = {"name": name, "direction": str(form.get("direction", "")).strip(),
              "enabled": form.get("enabled") == "on", "llm_rank": form.get("llm_rank") == "on"}
    for key in GROUP_FIELDS:
        if key not in form:  # partial clients must not erase unseen fields
            continue
        values = lines(str(form[key]))
        if len(values) > 200 or any(len(value) > 1000 for value in values):
            raise SettingsError("最多填写 200 条，每条不超过 1000 个字。", key)
        if key == "seed_dois":
            dois = [find_doi(value) for value in values]
            if not all(dois):
                raise SettingsError("有论文无法识别，请粘贴 DOI 或包含 DOI 的论文链接。", key)
            values = list(dict.fromkeys(dois))
        result[key] = values
    return result
