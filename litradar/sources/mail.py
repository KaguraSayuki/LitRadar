"""邮件接入:本地文件夹 / Maildir / IMAP(可插拔)。

Outlook 个人账号已禁用密码登录(服务器返回 ``LOGINDISABLED``),因此 IMAP 模式
适用于 Gmail / QQ / 163 这类支持应用专用密码的邮箱 —— 把 X-MOL 邮件从 Outlook
转发过去即可。
"""
from __future__ import annotations

import imaplib
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from ..config import MailConfig


@dataclass
class MailMessage:
    raw: bytes
    source_ref: str          # 稳定标识,用于幂等与留档
    origin: str              # 描述,如文件名 / UID


# ------------------------------------------------------------------ folder
def iter_folder(cfg: MailConfig) -> Iterator[MailMessage]:
    root = Path(cfg.folder)
    if not root.exists():
        return
    for p in sorted(root.glob("*.eml")):
        yield MailMessage(raw=p.read_bytes(), source_ref=f"file:{p.name}", origin=str(p))


def archive_folder(cfg: MailConfig, msg: MailMessage) -> None:
    """处理完后把文件移到 processed/,避免重复解析。"""
    if not cfg.move_processed_to:
        return
    src = Path(msg.origin)
    if not src.exists():
        return
    dst_dir = Path(cfg.move_processed_to)
    dst_dir.mkdir(parents=True, exist_ok=True)
    dst = dst_dir / src.name
    if dst.exists():
        dst = dst_dir / f"{src.stem}.{abs(hash(msg.raw)) % 10**6}{src.suffix}"
    shutil.move(str(src), str(dst))


# ----------------------------------------------------------------- maildir
def iter_maildir(cfg: MailConfig) -> Iterator[MailMessage]:
    root = Path(cfg.folder)
    for sub in ("new", "cur"):
        d = root / sub
        if not d.is_dir():
            continue
        for p in sorted(d.iterdir()):
            if p.is_file():
                yield MailMessage(raw=p.read_bytes(), source_ref=f"maildir:{sub}/{p.name}",
                                  origin=str(p))


# -------------------------------------------------------------------- imap
def iter_imap(cfg: MailConfig) -> Iterator[MailMessage]:
    """拉取 IMAP 邮件。用 BODY.PEEK 以免误标已读,处理成功后再显式标已读。"""
    if not cfg.imap_host or not cfg.imap_user:
        raise ValueError("mail.mode=imap 需要配置 imap_host / imap_user")
    pwd = cfg.imap_password
    if not pwd:
        raise ValueError(f"环境变量 {cfg.imap_password_env} 未设置(应用专用密码)")

    conn = imaplib.IMAP4_SSL(cfg.imap_host, cfg.imap_port)
    try:
        conn.login(cfg.imap_user, pwd)
        conn.select(cfg.imap_folder, readonly=False)
        typ, data = conn.search(None, cfg.imap_search or "ALL")
        if typ != "OK":
            return
        uids = data[0].split()
        for uid in uids:
            typ, fetched = conn.fetch(uid, "(BODY.PEEK[])")
            if typ != "OK" or not fetched or not isinstance(fetched[0], tuple):
                continue
            raw = fetched[0][1]
            yield MailMessage(raw=raw, source_ref=f"imap:{uid.decode()}",
                              origin=uid.decode())
    finally:
        try:
            conn.close()
        except Exception:
            pass
        conn.logout()


def mark_seen(cfg: MailConfig, msg: MailMessage) -> None:
    """IMAP 模式下把处理成功的邮件标记为已读。"""
    if cfg.mode != "imap" or not cfg.imap_mark_seen:
        return
    pwd = cfg.imap_password
    if not pwd:
        return
    conn = imaplib.IMAP4_SSL(cfg.imap_host, cfg.imap_port)
    try:
        conn.login(cfg.imap_user, pwd)
        conn.select(cfg.imap_folder, readonly=False)
        conn.store(msg.origin.encode(), "+FLAGS", "\\Seen")
    finally:
        try:
            conn.close()
        except Exception:
            pass
        conn.logout()


# ------------------------------------------------------------------ 统一入口
def iter_messages(cfg: MailConfig) -> Iterator[MailMessage]:
    mode = (cfg.mode or "folder").lower()
    if mode == "imap":
        yield from iter_imap(cfg)
    elif mode == "maildir":
        yield from iter_maildir(cfg)
    else:
        yield from iter_folder(cfg)


def acknowledge(cfg: MailConfig, msg: MailMessage) -> None:
    """入库成功后清理来源,保证幂等。"""
    mode = (cfg.mode or "folder").lower()
    if mode == "folder":
        archive_folder(cfg, msg)
    elif mode == "imap":
        mark_seen(cfg, msg)
