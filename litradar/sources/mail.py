"""邮件接入:本地文件夹 / Maildir / IMAP(可插拔)。

Outlook 个人账号已禁用密码登录(服务器返回 ``LOGINDISABLED``),因此 IMAP 模式
适用于 Gmail / QQ / 163 这类支持应用专用密码的邮箱 —— 把 X-MOL 邮件从 Outlook
转发过去即可。
"""
from __future__ import annotations

import imaplib
import shutil
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

from ..config import MailConfig

# IMAP 全程带超时:没有它,网络半挂时 systemd 的每日任务会一直吊在 read 上,
# 既不失败也不退出,下一轮还被单实例锁挡住 —— 等于雷达静默停摆。
IMAP_TIMEOUT = 30


@dataclass
class MailMessage:
    raw: bytes
    source_ref: str          # 稳定标识,用于幂等与留档
    origin: str              # 描述,如文件名 / UID
    # 处理成功后的回执。由生产者(iter_*)用闭包捕获当时的连接/路径,
    # 于是 acknowledge 不必为每封邮件重新登录一次 IMAP。
    ack: Callable[[], None] | None = field(default=None, repr=False, compare=False)


# ------------------------------------------------------------------ folder
def iter_folder(cfg: MailConfig) -> Iterator[MailMessage]:
    root = Path(cfg.folder)
    if not root.exists():
        return
    for p in sorted(root.glob("*.eml")):
        msg = MailMessage(raw=p.read_bytes(), source_ref=f"file:{p.name}", origin=str(p))
        msg.ack = lambda m=msg: archive_folder(cfg, m)
        yield msg


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
def imap_criteria(cfg: MailConfig) -> list[str]:
    """搜索条件。imaplib 把多个条件按 IMAP 语义 **AND** 起来。

    只在 imap_mark_seen 打开时才加 UNSEEN:那时每封处理过的邮件都会被标已读,
    UNSEEN 正好等于"还没处理过",流量不再随邮箱体积增长。反过来如果没人标已读
    却加了 UNSEEN,用户在网页上瞄一眼邮件就会让它永远进不来 —— 所以
    imap_mark_seen=false 时保持全量拉取,靠 message_id 去重兜底。
    """
    crit = [cfg.imap_search or "ALL"]
    if cfg.imap_mark_seen:
        crit.insert(0, "UNSEEN")
    return crit


def _connect(cfg: MailConfig, pwd: str) -> imaplib.IMAP4_SSL:
    conn = imaplib.IMAP4_SSL(cfg.imap_host, cfg.imap_port, timeout=IMAP_TIMEOUT)
    conn.login(cfg.imap_user, pwd)
    conn.select(cfg.imap_folder, readonly=False)
    return conn


def _disconnect(conn: imaplib.IMAP4_SSL) -> None:
    try:
        conn.close()
    except Exception:  # noqa: BLE001
        pass
    conn.logout()


def iter_imap(cfg: MailConfig) -> Iterator[MailMessage]:
    """拉取 IMAP 邮件。用 BODY.PEEK 以免误标已读,处理成功后再显式标已读。

    全程走 UID 命令。``SEARCH`` 默认返回的是**序列号**,它会随着新邮件到达、
    旧邮件被删而整体漂移;以前拿这个号去另一条连接里 STORE,中途只要收到一封
    新邮件,标已读就会打到不相干的邮件上。UID 在同一个 folder 内是稳定的。
    """
    if not cfg.imap_host or not cfg.imap_user:
        raise ValueError("mail.mode=imap 需要配置 imap_host / imap_user")
    pwd = cfg.imap_password
    if not pwd:
        raise ValueError(f"环境变量 {cfg.imap_password_env} 未设置(应用专用密码)")

    conn = _connect(cfg, pwd)
    try:
        typ, data = conn.uid("SEARCH", None, *imap_criteria(cfg))
        if typ != "OK" or not data or not data[0]:
            return
        for uid in data[0].split():
            typ, fetched = conn.uid("FETCH", uid, "(BODY.PEEK[])")
            if typ != "OK" or not fetched or not isinstance(fetched[0], tuple):
                continue
            uid_s = uid.decode()
            msg = MailMessage(raw=fetched[0][1], source_ref=f"imap:{uid_s}", origin=uid_s)
            if cfg.imap_mark_seen:
                # 生成器在两次 yield 之间是空闲的 —— pipeline 恰好在这个间隙调
                # acknowledge,所以回执可以直接借用这条已经开着的连接发 STORE,
                # 不必像以前那样每封邮件 login/select/store/logout 一整轮。
                def _ack(u: bytes = uid, c: imaplib.IMAP4_SSL = conn) -> None:
                    c.uid("STORE", u, "+FLAGS", "\\Seen")

                msg.ack = _ack
            yield msg
    finally:
        _disconnect(conn)


def mark_seen(cfg: MailConfig, msg: MailMessage) -> None:
    """IMAP 模式下把处理成功的邮件标记为已读。

    正常路径是 ``msg.ack``(复用 iter_imap 那条连接),这里只是兜底:调用方
    自己拼了 MailMessage 时才走到。用 UID 而非序列号,所以即使换了连接也安全。
    """
    if cfg.mode != "imap" or not cfg.imap_mark_seen:
        return
    pwd = cfg.imap_password
    if not pwd:
        return
    conn = _connect(cfg, pwd)
    try:
        conn.uid("STORE", msg.origin.encode(), "+FLAGS", "\\Seen")
    finally:
        _disconnect(conn)


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
    """入库成功后清理来源,保证幂等。

    优先用生产者留下的回执(它带着当时那条连接/那个路径),一次 ingest 因此
    只建 1 条 IMAP 连接;没有回执时才按模式兜底。
    """
    if msg.ack is not None:
        msg.ack()
        return
    mode = (cfg.mode or "folder").lower()
    if mode == "folder":
        archive_folder(cfg, msg)
    elif mode == "imap":
        mark_seen(cfg, msg)
