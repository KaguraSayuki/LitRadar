"""命令行入口: ``litradar <command>``。"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from . import db, enrich, pipeline, rank, summarize
from .config import load_config
from .lock import AlreadyRunning, single_instance
from .sources import easyscholar, mail, xmol_email


def _lock_path(cfg):
    return cfg.db_file.parent / "litradar.lock"


def cmd_init_db(cfg, args):
    db.Database(cfg.db_file).init()
    print(f"数据库已初始化: {cfg.db_file}")
    return 0


def cmd_parse(cfg, args):
    """只解析邮件并打印,不入库 —— 用于校准解析器。"""
    n = 0
    for msg in mail.iter_messages(cfg.mail):
        records, meta = xmol_email.parse_bytes(msg.raw)
        print(f"--- {msg.source_ref} | {meta.get('subject')} | {meta.get('received_at')}")
        print(xmol_email.dumps(records))
        n += len(records)
    print(f"共 {n} 条", file=sys.stderr)
    return 0


def cmd_ingest(cfg, args):
    if args.what in ("mail", "all"):
        print(pipeline.ingest_mail(cfg))
    if args.what in ("search", "all"):
        print(pipeline.ingest_keyword_search(cfg))
    return 0


def cmd_enrich(cfg, args):
    print(json.dumps(enrich.run(cfg, limit=args.limit), ensure_ascii=False, indent=2))
    return 0


def cmd_rank(cfg, args):
    print(json.dumps(rank.run(cfg, days=args.days), ensure_ascii=False, indent=2))
    return 0


def cmd_summarize(cfg, args):
    print(json.dumps(
        summarize.run(cfg, days=args.days, limit=args.limit, force=args.force),
        ensure_ascii=False, indent=2))
    return 0


def cmd_run(cfg, args):
    out = pipeline.run_all(cfg, days=args.days)
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0





def cmd_stats(cfg, args):
    conn = db.Database(cfg.db_file).connect()
    try:
        print(json.dumps(db.stats(conn), ensure_ascii=False, indent=2))
    finally:
        conn.close()
    return 0


def cmd_mail_test(cfg, args):
    """只读测试 IMAP:能不能登进去、找不找得到 X-MOL 邮件、解析出什么。

    **不改任何状态**:不标已读、不移动邮件、不写库。配完邮箱先跑这个。
    """
    import imaplib

    m = cfg.mail
    print("\n【邮件接入测试】(只读,不标记已读、不改动任何邮件)\n")
    print(f"  模式      : {m.mode}")
    if m.mode != "imap":
        print(f"  ⚠️  当前不是 imap 模式,实际读的是目录 {cfg.inbox_dir}")
        n = len(list(cfg.inbox_dir.glob("*.eml"))) if cfg.inbox_dir.exists() else 0
        print(f"      目录里待处理 .eml:{n} 封")
        print('\n  要改用 IMAP,把 config.yaml 的 mail.mode 改成 "imap"。')
        return 0

    print(f"  服务器    : {m.imap_host}:{m.imap_port}")
    print(f"  账号      : {m.imap_user or '(未设)'}")
    print(f"  搜索式    : {m.imap_search}")
    print(f"  密码来源  : 环境变量 {m.imap_password_env}")

    if not m.imap_user:
        print("\n  ❌ 未配置 imap_user。在 config.yaml 的 mail 段填收件邮箱。")
        return 1
    if not m.imap_password:
        print(f"\n  ❌ 环境变量 {m.imap_password_env} 未设置(.env 里填应用专用密码/授权码)。")
        return 1
    print("  密码      : 已设置\n")

    try:
        # 带超时:服务器半挂时不要让体检命令一直吊在这里
        conn = imaplib.IMAP4_SSL(m.imap_host, m.imap_port,
                                 timeout=mail.IMAP_TIMEOUT)
    except Exception as e:  # noqa: BLE001
        print(f"  ❌ 连接失败: {type(e).__name__}: {e}")
        return 1

    try:
        try:
            conn.login(m.imap_user, m.imap_password)
            print("  ✅ 登录成功")
        except imaplib.IMAP4.error as e:
            msg = str(e)
            print(f"  ❌ 登录失败: {msg}")
            if "AUTHENTICATIONFAILED" in msg or "Invalid credentials" in msg:
                print("     常见原因:")
                print("       · 用的不是【应用专用密码/授权码】,而是登录密码")
                print("       · 邮箱未开启 IMAP 服务")
                print("       · Gmail 需先开两步验证才能生成应用专用密码")
                print("       · QQ 邮箱授权码在 设置→账户 里生成")
            return 1

        conn.select(m.imap_folder, readonly=True)          # 只读!
        typ, data = conn.search(None, m.imap_search or "ALL")
        if typ != "OK":
            print(f"  ❌ 搜索失败: {typ}")
            return 1
        uids = data[0].split()
        print(f"  ✅ 在 {m.imap_folder} 找到 {len(uids)} 封匹配邮件")
        if not uids:
            print("\n  ⚠️  一封都没找到。检查:")
            print("       · Outlook 的转发规则建了吗(发件人含 newsletter.x-mol.com)")
            print("       · 转发是否真的送达(先去网页邮箱确认)")
            print(f"       · 搜索式是否匹配:{m.imap_search}")
            return 1

        print("\n  最近 5 封:")
        total = 0
        for uid in uids[-5:]:
            typ, fetched = conn.fetch(uid, "(BODY.PEEK[])")   # PEEK:不标已读
            if typ != "OK" or not fetched or not isinstance(fetched[0], tuple):
                continue
            recs, meta = xmol_email.parse_bytes(fetched[0][1])
            subj = (meta.get("subject") or "")[:38]
            date = (meta.get("received_at") or "")[:31]
            flag = f"解析出 {len(recs)} 条" if recs else "⚠️ 0 条(可能不是订阅邮件)"
            print(f"      [{date}] {subj}  →  {flag}")
            for r in recs[:3]:
                print(f"          · {r.title[:62]}")
            total += len(recs)

        print(f"\n  合计可解析条目:{total}")
        if total == 0:
            print("  ⚠️  能登进去但解析不出条目 —— 可能邮件模板变了。")
            print("      把一封原始邮件存成 .eml 给我,我按真实样本校准解析器。")
        else:
            print('\n  ✅ 一切正常。把 config.yaml 的 mail.mode 设为 "imap" 后跑 ingest 即可。')
        return 0
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass
        conn.logout()


# ------------------------------------------------------------------ 体检
OK, BAD, WARN = "✅", "❌", "⚠️ "


def _journal_rank_line(cfg) -> tuple[str, str, str]:
    """期刊等级密钥的体检结论 ``(mark, label, detail)``。

    这里刻意不把"没配密钥"算成 problem:期刊等级是可选增强,不配也能跑完
    整条流水线。但必须显式说清后果 —— 以前这一项压根不检查,缺密钥时体检
    全绿,而卡片上的影响因子/分区永远是空的,用户无从判断是接口挂了还是
    自己没配。
    """
    env_name = cfg.journal_rank.api_key_env or easyscholar.DEFAULT_KEY_ENV
    if not cfg.journal_rank.enabled:
        return OK, "期刊等级已关闭", "journal_rank.enabled = false"
    # 直接问客户端,而不是自己 os.environ.get:否则"只有空格"这种值会让
    # check 报已设置、而 enrich 认为没有密钥,又是一次"体检说没问题"的误导。
    if easyscholar.api_key(env_name):
        return OK, f"{env_name} 已设置", "查询结果会缓存进 journal_rank 表"
    return (WARN, f"{env_name} 未设置",
            "影响因子/中科院分区不会显示;不配也能正常跑,只是缺这些标签")


def cmd_check(cfg, args):
    """体检:配置 / 密钥 / 数据库 / 网络 / LLM 连通性。

    配完 key 后跑这个,一次看清哪里没通。
    """
    from pathlib import Path

    from .config import ROOT
    from .llm import DeepSeek, LLMError
    from .rank import load_interests

    problems: list[str] = []

    def line(mark: str, label: str, detail: str = "") -> None:
        print(f"  {mark} {label}" + (f"  {detail}" if detail else ""))

    print("\n【1】配置文件")
    cfg_path = ROOT / "config.yaml"
    if cfg_path.exists():
        line(OK, "config.yaml", str(cfg_path))
    else:
        line(WARN, "config.yaml 不存在", "正在使用默认值;建议 cp config.example.yaml config.yaml")
        problems.append("缺少 config.yaml")

    print("\n【2】密钥与环境变量 (.env)")
    env_path = ROOT / ".env"
    if env_path.exists():
        if os.name == "nt":
            # Windows 的 st_mode 不反映 ACL,chmod 也只能切换只读位。在这里
            # 按 600 判断会报一个用户无法修复的告警,所以只确认存在。
            line(OK, ".env", "已存在(Windows 权限由 ACL 控制,请勿共享该文件)")
        else:
            mode = oct(env_path.stat().st_mode)[-3:]
            line(OK if mode == "600" else WARN, ".env", f"权限 {mode}" +
                 ("" if mode == "600" else "  ← 建议 chmod 600 .env"))
    else:
        line(WARN, ".env 不存在", "复制 .env.example 并填写")
        problems.append("缺少 .env")

    # DeepSeek
    llm = DeepSeek(cfg.llm)
    if cfg.llm.api_key:
        k = cfg.llm.api_key
        line(OK, f"{cfg.llm.api_key_env} 已设置",
             f"{k[:6]}…{k[-4:]} (长度 {len(k)})")
    else:
        line(BAD, f"{cfg.llm.api_key_env} 未设置",
             "没有它排序与摘要会跳过 LLM,只按 BM25+规则排")
        problems.append(f"{cfg.llm.api_key_env} 未设置")

    # 可选
    for name, why in [("S2_API_KEY", "Semantic Scholar 限流会宽松很多,建议申请"),
                      ("OPENALEX_API_KEY", "只有开了 sources.openalex_enabled 才需要")]:
        if os.environ.get(name):
            line(OK, f"{name} 已设置")
        else:
            line(WARN, f"{name} 未设置", why)

    # 期刊等级同样可选,但缺密钥时是"静默降级":卡片上永远不会有影响因子/
    # 分区标签,而且富化统计全是 0,不主动说一句用户根本看不出哪里不对。
    line(*_journal_rank_line(cfg))

    # 绑定地址与口令:没有账号体系,只看"是否暴露到局域网"
    if cfg.app.host in ("127.0.0.1", "localhost"):
        line(OK, "仅绑本机", f"{cfg.app.host}:{cfg.app.port} · 不设口令也安全")
        if cfg.app.token:
            line(OK, "接口口令已设置", "本机访问也需要 ?k=<token>")
    else:
        line(WARN, "绑定了非本机地址", f"{cfg.app.host}:{cfg.app.port}")
        if cfg.app.token:
            line(OK, "接口口令已设置", "访问时带 ?k=<token>")
        else:
            line(BAD, "暴露到局域网但未设口令",
                 "同网段任何人都能调 /admin/run/* 花掉你的 DeepSeek 额度。"
                 "建议设 LITRADAR_TOKEN")
            problems.append("绑 0.0.0.0 但未设 LITRADAR_TOKEN")

    print("\n【3】检索词与偏好")
    prof = load_interests(cfg)
    if not cfg.interests_data:
        line(BAD, "interests.yaml 未加载", str(cfg.interests_file))
        problems.append("interests.yaml 未加载")
    else:
        line(OK, "已加载",
             f"核心词 {len(prof.core)} / 加分词 {len(prof.bonus)} / 期刊 {len(prof.journals_core)}")
        import re as _re
        qs = prof.queries
        if not qs:
            line(WARN, "profile 未设 search_queries",
                 "会回退到核心+加分关键词拼接")
        else:
            cn = [q for q in qs if _re.search(r"[\u4e00-\u9fff]", q)]
            if cn:
                line(BAD, f"{len(cn)} 条 search_queries 含中文",
                     "Crossref 检索会返回 0 条,必须改成英文")
                problems.append("search_queries 含中文")
            else:
                line(OK, f"检索词 {len(qs)} 条(取并集)",
                     f"含 {len(prof.exclude_title_prefixes)} 条非论文前缀过滤")
                for q in qs:
                    print(f"        · {q[:88]}")

    print("\n【4】数据库")
    if cfg.db_file.exists():
        conn = db.Database(cfg.db_file).connect()
        try:
            n = conn.execute("SELECT COUNT(*) FROM item").fetchone()[0]
            withabs = conn.execute(
                "SELECT COUNT(*) FROM item WHERE abstract IS NOT NULL AND abstract<>''"
            ).fetchone()[0]
            scored = conn.execute("SELECT COUNT(*) FROM score").fetchone()[0]
            summ = conn.execute("SELECT COUNT(*) FROM summary").fetchone()[0]
            line(OK, "数据库", str(cfg.db_file))
            line(OK if n else WARN, "条目 / 有摘要 / 已评分 / 已摘要",
                 f"{n} / {withabs} / {scored} / {summ}")
            if n and not scored:
                line(WARN, "有数据但未评分", "跑一次 rank")
        finally:
            conn.close()
    else:
        line(BAD, "数据库不存在", "运行 litradar init-db")
        problems.append("数据库未初始化")

    print("\n【5】邮件接入")
    line(OK, "模式", cfg.mail.mode)
    if cfg.mail.mode == "folder":
        d = cfg.inbox_dir
        if d.exists():
            n = len(list(d.glob("*.eml")))
            line(OK, "收件目录", f"{d} (待处理 {n} 封)")
        else:
            line(WARN, "收件目录不存在", str(d))
    elif cfg.mail.mode == "imap":
        if cfg.mail.imap_password:
            line(OK, "IMAP 密码已设置")
        else:
            line(BAD, f"{cfg.mail.imap_password_env} 未设置",
                 "Gmail/QQ 需要用【应用专用密码/授权码】,不是登录密码")
            problems.append("IMAP 密码未设置")
        line(OK, "IMAP 服务器", f"{cfg.mail.imap_host}:{cfg.mail.imap_port}")

    print("\n【6】网络可达性")
    import requests
    # 注意:S2 不在这里探测。它下面【6b】会单独测一次 —— 两处都发的话
    # 两次请求间隔不到 1 秒,会撞上 S2 的 1 req/s 限流(实测踩过)。
    for name, url in [("Crossref", "https://api.crossref.org/works?rows=1")]:
        try:
            r = requests.get(url, timeout=20,
                             headers={"User-Agent": "LitRadar/0.1 (health check)"})
            line(OK if r.status_code < 500 else WARN, name, f"HTTP {r.status_code}")
        except Exception as e:  # noqa: BLE001
            line(BAD, name, str(e)[:70])
            problems.append(f"{name} 不可达")

    print("\n【6b】Semantic Scholar 配额")
    s2_key = os.environ.get("S2_API_KEY")
    if not s2_key:
        line(WARN, "未配 S2_API_KEY",
             "走共享池,约 1 req/s 且容易 429。免费申请能显著改善")
    else:
        try:
            r = requests.get(
                "https://api.semanticscholar.org/graph/v1/paper/DOI:10.1021/acs.orglett.6c03259",
                params={"fields": "title"},
                headers={"User-Agent": "LitRadar/0.1", "x-api-key": s2_key},
                timeout=25,
            )
            if r.status_code == 200:
                lim = r.headers.get("x-ratelimit-limit", "?")
                rem = r.headers.get("x-ratelimit-remaining", "?")
                itv = r.headers.get("x-ratelimit-interval", "?")
                line(OK, "S2 key 生效",
                     f"额度 {rem}/{lim} 每 {itv} · 已认证专用池")
            elif r.status_code == 401:
                # 官方定义:401 = credentials are invalid
                line(BAD, "S2 key 无效 (401)",
                     "官方语义是'凭据无效'—— 检查是否复制完整、有无多余空格")
                problems.append("S2_API_KEY 无效(401)")
            elif r.status_code == 403:
                # 官方定义:403 = 请求被理解但无权限。key 本身可能是对的,
                # 只是还没激活/审核未完成 —— 与 401 的处理方式完全不同。
                line(WARN, "S2 key 暂未被授权 (403)",
                     "官方语义是'拒绝访问'而非'凭据无效';通常是审核/激活未完成。"
                     "富化会自动降级为匿名访问")
            elif r.status_code == 429:
                line(WARN, "S2 限流 (429)", "稍后重试即可")
            else:
                line(WARN, f"S2 返回 HTTP {r.status_code}", str(r.text)[:80])
        except Exception as e:  # noqa: BLE001
            line(BAD, "S2 测试失败", f"{type(e).__name__}: {str(e)[:70]}")
            problems.append("S2 测试失败")

    print("\n【7】DeepSeek 连通性")
    if not llm.available:
        line(WARN, "跳过", "未配置 API key")
    else:
        try:
            got = llm.json(
                "你只输出 JSON。",
                '请只输出 {"ok":true,"msg":"pong"}',
                max_tokens=64,
            )
            line(OK, "DeepSeek 调用成功", f"模型 {cfg.llm.model} · 返回 {got}")
        except LLMError as e:
            line(BAD, "DeepSeek 调用失败", str(e)[:110])
            problems.append("DeepSeek 调用失败")
        except Exception as e:  # noqa: BLE001
            line(BAD, "DeepSeek 调用异常", f"{type(e).__name__}: {str(e)[:100]}")
            problems.append("DeepSeek 调用异常")

    print()
    if problems:
        print(f"⚠️  发现 {len(problems)} 个问题:")
        for p in problems:
            print(f"    · {p}")
    else:
        print("🎉 全部检查通过,可以跑 litradar run")
    print()
    return 0 if not problems else 1


DAYS_HELP = "时间窗(天)。不传则取 config 的 app.pipeline_window_days"


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="litradar", description="个人化学文献雷达")
    p.add_argument("-c", "--config", default=None, help="config.yaml 路径")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init-db", help="初始化数据库").set_defaults(func=cmd_init_db)

    sp = sub.add_parser("parse", help="只解析邮件(校准解析器用)")
    sp.set_defaults(func=cmd_parse)

    sp = sub.add_parser("ingest", help="采集")
    sp.add_argument("what", choices=["mail", "search", "all"], nargs="?", default="all")
    sp.set_defaults(func=cmd_ingest)

    sp = sub.add_parser("enrich", help="富化(补摘要/引用数)")
    sp.add_argument("--limit", type=int, default=300)
    sp.set_defaults(func=cmd_enrich)

    sp = sub.add_parser("rank", help="排序")
    sp.add_argument("--days", type=int, default=None, help=DAYS_HELP)
    sp.set_defaults(func=cmd_rank)

    sp = sub.add_parser("summarize", help="生成中文摘要")
    sp.add_argument("--days", type=int, default=None, help=DAYS_HELP)
    sp.add_argument("--limit", type=int, default=200)
    sp.add_argument("--force", action="store_true",
                    help="重做已有摘要(默认只补缺失的)")
    sp.set_defaults(func=cmd_summarize)

    sp = sub.add_parser("run", help="跑完整流水线")
    sp.add_argument("--days", type=int, default=None, help=DAYS_HELP)
    sp.set_defaults(func=cmd_run)

    sub.add_parser("stats", help="查看统计").set_defaults(func=cmd_stats)
    sub.add_parser("mail-test", help="只读测试 IMAP 邮件接入").set_defaults(func=cmd_mail_test)
    sub.add_parser("check", help="体检:配置/密钥/数据库/网络/LLM 连通性").set_defaults(func=cmd_check)
    return p


def _configure_console() -> None:
    """让中文输出在 Windows 上被重定向时也不炸。

    Windows 控制台本身走 UTF-8(PEP 528),但一旦输出被重定向到文件或管道
    (任务计划、``> log.txt``),Python 会退回 ANSI 代码页 —— 英文系统是
    cp1252,打印中文直接 UnicodeEncodeError。这里统一按 UTF-8 输出,并把
    编码错误降级为替换字符,宁可少一个词也不要整条命令崩掉。
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:          # 被 pytest 等替换过的流
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            pass


def main(argv: list[str] | None = None) -> int:
    _configure_console()
    args = build_parser().parse_args(argv)
    cfg = load_config(args.config)
    # --days 缺省时统一取配置,避免"定时任务 200 天、手工跑 30 天"这种不一致
    if getattr(args, "days", None) is None:
        args.days = cfg.app.pipeline_window_days
    # 会打 API 的阶段加互斥锁,避免定时任务重叠导致限流互抢
    guarded = args.cmd in ("run", "ingest", "enrich", "rank", "summarize")
    try:
        if guarded:
            with single_instance(_lock_path(cfg)):
                return args.func(cfg, args)
        return args.func(cfg, args)
    except AlreadyRunning as e:
        print(f"跳过: {e}", file=sys.stderr)
        return 0
    except KeyboardInterrupt:
        return 130
    except Exception as e:  # noqa: BLE001
        print(f"错误: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
