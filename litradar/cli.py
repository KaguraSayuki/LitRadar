"""命令行入口: ``litradar <command>``。"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from . import credentials, db, enrich, execution, pipeline, rank, summarize
from .config import load_config, read_secret
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


def _pick_group(cfg, slug):
    """把 --group 的 slug 解析成一个 Profile;没给就返回 None(全部启用的组)。"""
    if not slug:
        return None
    from .rank import load_groups

    groups = load_groups(cfg)
    for group in groups:
        if group.slug == slug:
            return group
    known = "、".join(g.slug for g in groups) or "(无)"
    raise ValueError(f"未知订阅组 {slug!r};当前有:{known}")


def _result_exit_code(*results: dict) -> int:
    """分组允许失败后继续,但已报告的错误仍要让脚本/systemd 判定失败。

    调用方传各阶段汇总;不递归查询词/组名映射(其中也可能有名为 errors 的键)。
    未配置可选来源等 skipped 结果仍算正常完成。
    """
    return int(any(result.get("errors", 0) for result in results))


def cmd_ingest(cfg, args):
    out = {}
    if args.what in ("mail", "all"):
        out["mail"] = pipeline.ingest_mail(cfg)
        print(out["mail"])
    if args.what in ("search", "all"):
        out["search"] = pipeline.ingest_keyword_search(cfg, group=_pick_group(cfg, args.group))
        print(out["search"])
    return _result_exit_code(*out.values())


def cmd_enrich(cfg, args):
    print(json.dumps(enrich.run(cfg, limit=args.limit), ensure_ascii=False, indent=2))
    return 0


def cmd_rank(cfg, args):
    out = rank.run(cfg, days=args.days, group=_pick_group(cfg, args.group))
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return _result_exit_code(out)


def cmd_summarize(cfg, args):
    out = summarize.run(cfg, days=args.days, limit=args.limit, force=args.force,
                        group=_pick_group(cfg, args.group))
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return _result_exit_code(out)


def cmd_run(cfg, args):
    out = pipeline.run_all(cfg, days=args.days, group=_pick_group(cfg, args.group))
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return _result_exit_code(*out.values())





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
        conn = mail.open_imap(m)
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
        mail._disconnect(conn)


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


def cmd_admin_password(cfg, args):
    """设置 / 清除管理员密码，只把 PBKDF2 哈希写进私有凭据文件。"""
    from . import passwords
    name = cfg.app.admin_password_env

    if args.clear:
        credentials.write(cfg, name, None)
        print("已清除应用管理的访问密码，新请求立即生效。")
        return 0

    import getpass

    first = getpass.getpass("新密码(输入不回显): ")
    if len(first) < passwords.MIN_PASSWORD_LENGTH:
        print(f"错误:密码至少 {passwords.MIN_PASSWORD_LENGTH} 位。", file=sys.stderr)
        return 1
    if first != getpass.getpass("再输一次: "):
        print("错误:两次输入不一致。", file=sys.stderr)
        return 1

    credentials.write(cfg, name, passwords.hash_password(first))
    print("已保存管理员密码哈希，新请求立即生效；已有登录需要重新验证。")
    stages = "、".join(cfg.admin.guarded_stages) or "(空)"
    print(f"受保护阶段:{stages}"
          f"(冷却 {cfg.admin.cooldown_seconds}s,每日上限 {cfg.admin.daily_limit} 次)")
    return 0


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
    cfg_path = cfg.config_file or ROOT / "config.yaml"
    if cfg_path.exists():
        line(OK, "config.yaml", str(cfg_path))
    else:
        line(WARN, "config.yaml 不存在", "正在使用默认值；在网页保存设置后自动创建")

    print("\n【2】凭据与部署环境")
    env_path = cfg_path.parent / ".env"
    private_path = credentials.store_path(cfg)
    if private_path.exists():
        line(OK, "应用管理的凭据文件", "已存在；可在网页更换，下次调用生效")
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
        line(OK, ".env 未使用", "可通过网页接入服务，不需要创建此兼容文件")

    # DeepSeek
    llm = DeepSeek(cfg.llm)
    if cfg.llm.api_key:
        line(OK, f"{cfg.llm.api_key_env} 已设置")
    else:
        line(BAD, f"{cfg.llm.api_key_env} 未设置",
             "没有它排序与摘要会跳过 LLM,只按 BM25+规则排")
        if cfg.llm.enabled:
            problems.append(f"{cfg.llm.api_key_env} 未设置")

    # 可选
    for name, why in [("S2_API_KEY", "Semantic Scholar 限流会宽松很多,建议申请"),
                      ("OPENALEX_API_KEY", "只有开了 sources.openalex_enabled 才需要")]:
        if read_secret(name):
            line(OK, f"{name} 已设置")
        else:
            line(WARN, f"{name} 未设置", why)

    # 期刊等级同样可选,但缺密钥时是"静默降级":卡片上永远不会有影响因子/
    # 分区标签,而且富化统计全是 0,不主动说一句用户根本看不出哪里不对。
    line(*_journal_rank_line(cfg))

    # These are configured listening values; a reverse proxy may expose another URL.
    if cfg.app.is_loopback:
        line(OK, "配置为仅绑本机", f"{cfg.app.host}:{cfg.app.port}")
        if cfg.app.token:
            line(OK, "接口口令已设置", "本机访问也需要 ?k=<token>")
    else:
        line(WARN, "绑定了非本机地址", f"{cfg.app.host}:{cfg.app.port}")
        if cfg.app.token or cfg.app.admin_password_hash:
            line(OK, "访问保护已设置", "网页可使用访问密码登录，脚本可使用接口口令")
        else:
            line(BAD, "暴露到局域网但未设口令",
                 "手动触发接口现在会直接拒绝(fail closed);"
                 f"请设 {cfg.app.token_env},或把 app.host 改回 127.0.0.1")
            problems.append(f"绑了 {cfg.app.host} 但未设 {cfg.app.token_env}")

    # 花钱阶段(默认 rank / summarize / all)的护栏
    guarded = "、".join(cfg.admin.guarded_stages)
    if not cfg.admin.guarded_stages:
        line(WARN, "花钱阶段没有护栏", "admin.guarded_stages 为空,密码与频率限制都不生效")
    else:
        if cfg.app.admin_password_hash:
            line(OK, f"{cfg.app.admin_password_env} 已设置",
                 f"登录会话可运行 {guarded}；仅用接口口令时另需管理员密码")
        else:
            line(WARN, f"{cfg.app.admin_password_env} 未设置",
                 f"只要拿到接口口令就能运行 {guarded} 烧 DeepSeek 额度;"
                 " 用 litradar admin-password 设置")
        limits = []
        if cfg.admin.cooldown_seconds:
            limits.append(f"冷却 {cfg.admin.cooldown_seconds}s")
        if cfg.admin.daily_limit:
            limits.append(f"每阶段每日上限 {cfg.admin.daily_limit} 次")
        line(OK if limits else WARN, f"{guarded} 的频率限制",
             "、".join(limits) if limits else "冷却与每日上限都是 0(不限制)")

    # 订阅组:几个方向、各自的 LLM 精排开关(这是唯一按组花钱的一步)。
    # 频率限制按**组**记账,所以这里把组名一并列出来。
    from .rank import load_groups
    try:
        groups = load_groups(cfg)
    except ValueError as e:
        groups = []
        line(BAD, "订阅组配置有误", str(e)[:80])
        problems.append("interests.yaml 的 groups 无法解析")
    if groups:
        detail = "、".join(
            f"{g.name}({'精排开' if g.llm_rank else '精排关'}"
            f"{'' if g.enabled else '·已停用'})" for g in groups)
        line(OK, f"订阅组 {len(groups)} 个", detail[:110])

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
    s2_key = read_secret("S2_API_KEY")
    if not s2_key:
        line(WARN, "未配 S2_API_KEY",
             "走共享池,约 1 req/s 且容易 429。免费申请能显著改善")
    else:
        try:
            r = requests.get(
                "https://api.semanticscholar.org/graph/v1/paper/DOI:10.18653/v1/N18-3011",
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
                line(WARN, f"S2 返回 HTTP {r.status_code}", "请检查凭据和服务状态")
        except Exception as e:  # noqa: BLE001
            line(BAD, "S2 测试失败", "请检查网络和服务状态")
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
            line(OK, "DeepSeek 调用成功", f"模型 {cfg.llm.model}")
        except LLMError as e:
            line(BAD, "DeepSeek 调用失败", "请检查模型、服务地址、密钥和可用额度")
            problems.append("DeepSeek 调用失败")
        except Exception as e:  # noqa: BLE001
            line(BAD, "DeepSeek 调用异常", "请检查模型服务配置")
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




def cmd_scheduler(cfg, args):
    from .scheduler import serve
    serve(cfg.config_file)
    return 0


def cmd_schedule_handoff(cfg, args):
    from .settings import SettingsStore
    if not args.external_timers_stopped:
        raise ValueError("请先停用此实例的 systemd、launchd 或 Windows 定时任务，确认后使用 --external-timers-stopped。")
    store = SettingsStore(cfg)
    store.update_config({'schedule.owner': 'application', 'schedule.handoff_confirmed': True,
                         'schedule.enabled': False}, store.version())
    print("已确认由应用管理调度。请在网页选择时间并开启自动更新。")
    return 0


def cmd_serve(cfg, args):
    import multiprocessing
    import uvicorn
    from .scheduler import serve
    context = multiprocessing.get_context('spawn')
    stop = context.Event()
    worker = context.Process(target=serve, args=(str(cfg.config_file), stop), daemon=True)
    if cfg.config_file:
        os.environ['LITRADAR_CONFIG'] = str(cfg.config_file)
    worker.start()
    try:
        uvicorn.run('litradar.web.app:app', host=args.host or cfg.app.host,
                    port=args.port or cfg.app.port)
    finally:
        stop.set()
        worker.join(timeout=10)
        if worker.is_alive():
            worker.terminate()
            worker.join(timeout=5)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="litradar", description="个人化学文献雷达")
    p.add_argument("-c", "--config", default=None, help="config.yaml 路径")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init-db", help="初始化数据库").set_defaults(func=cmd_init_db)
    sub.add_parser('scheduler', help='运行独立的自动更新进程').set_defaults(func=cmd_scheduler)
    sp = sub.add_parser('schedule-handoff', help='确认已停用外部定时任务，由应用接管调度')
    sp.add_argument('--external-timers-stopped', action='store_true')
    sp.set_defaults(func=cmd_schedule_handoff)
    sp = sub.add_parser('serve', help='启动网页和独立的自动更新进程')
    sp.add_argument('--host', default=None)
    sp.add_argument('--port', type=int, default=None)
    sp.set_defaults(func=cmd_serve)

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
    sp = sub.add_parser("admin-password",
                        help="设置/清除花钱阶段的管理员密码(只存哈希)")
    sp.add_argument("--clear", action="store_true", help="删除已设置的密码")
    sp.set_defaults(func=cmd_admin_password)
    for name in ("ingest", "rank", "summarize", "run"):
        parser = sub.choices.get(name)
        if parser is not None and not any(
                a.dest == "group" for a in parser._actions):
            parser.add_argument("--group", default=None, metavar="SLUG",
                                help="只跑这一个订阅组(默认全部启用的组)")
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
            with single_instance(_lock_path(cfg)), credentials.snapshot(cfg):
                stage = {"run": "all", "ingest": "search"}.get(args.cmd, args.cmd)
                groups = ([_pick_group(cfg, args.group)] if getattr(args, 'group', None)
                          else [g for g in rank.load_groups(cfg) if g.enabled])
                if args.cmd == "ingest":
                    if args.what in ("mail", "all"):
                        execution.check_limits(cfg, "mail")
                    if args.what in ("search", "all"):
                        execution.check_groups(cfg, "search", groups)
                else:
                    execution.check_groups(cfg, stage, groups)
                return args.func(cfg, args)
        with credentials.snapshot(cfg):
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
