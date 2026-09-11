#!/usr/bin/env python
"""给 LitRadar 各页面在多种屏幕尺寸下截图,用于检查响应式布局。

用法:
    .venv/bin/python scripts/screenshot.py                      # 全部尺寸 + 全部页面
    .venv/bin/python scripts/screenshot.py -w 390 -p /          # 只截某个尺寸的首页
    .venv/bin/python scripts/screenshot.py --dark               # 深色模式
    .venv/bin/python scripts/screenshot.py --list               # 看预设

依赖:
    .venv/bin/pip install playwright && .venv/bin/playwright install chromium

为什么需要它:布局问题(导航被挤成竖排单字、卡片三栏挤压)在桌面宽度下
根本看不出来,必须按真实手机宽度渲染一遍。
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Chromium 装在项目内(沙箱不让我们写 ~/.cache),这里默认指过去。
# 若你装到了别处,export PLAYWRIGHT_BROWSERS_PATH=... 覆盖即可。
os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(ROOT / ".playwright"))

# 预设尺寸(宽, 高, 名称)
VIEWPORTS = {
    "phone-sm": (360, 740, "小屏安卓"),
    "phone":    (390, 844, "iPhone 14/15"),
    "phone-lg": (430, 932, "iPhone Pro Max"),
    "tablet":   (768, 1024, "iPad 竖屏"),
    "desktop":  (1280, 800, "桌面"),
}

PAGES = {
    "inbox":     "/",
    "item":      "/item/{first_id}",   # 需要动态取一个 id
    "interests": "/interests",
    "stats":     "/stats",
    "week":      "/week",
}


def base_url() -> str:
    """默认走本机应用端口,避免自签证书让浏览器报错。"""
    return os.environ.get("LITRADAR_URL", "http://127.0.0.1:8090")


def token() -> str:
    from litradar.config import load_config

    return load_config(ROOT / "config.yaml").app.token or ""


def first_item_id() -> int | None:
    from litradar import db
    from litradar.config import load_config

    cfg = load_config(ROOT / "config.yaml")
    conn = db.Database(cfg.db_file).connect()
    try:
        row = conn.execute(
            "SELECT id FROM item i LEFT JOIN score s ON s.item_id=i.id "
            "ORDER BY COALESCE(s.final_score,-1) DESC LIMIT 1"
        ).fetchone()
        return int(row[0]) if row else None
    finally:
        conn.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("-w", "--width", type=int, help="只截这个宽度")
    ap.add_argument("-p", "--page", help="只截这个路径")
    ap.add_argument("-o", "--out", default="/tmp/litradar-shots")
    ap.add_argument("--full", action="store_true", help="整页截图(默认只截首屏)")
    ap.add_argument("--dark", action="store_true", help="按系统深色模式渲染(页面跟随 prefers-color-scheme)")
    ap.add_argument("--list", action="store_true")
    args = ap.parse_args()

    if args.list:
        print("尺寸预设:")
        for k, (w, h, desc) in VIEWPORTS.items():
            print(f"  {k:10s} {w}x{h}  {desc}")
        print("\n页面:")
        for k, v in PAGES.items():
            print(f"  {k:10s} {v}")
        return 0

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("未安装 playwright。执行:\n"
              "  .venv/bin/pip install playwright\n"
              "  .venv/bin/playwright install chromium", file=sys.stderr)
        return 1

    outdir = Path(args.out)
    outdir.mkdir(parents=True, exist_ok=True)
    t = token()
    url_root = base_url()

    item_id = first_item_id()
    pages = {}
    for name, path in PAGES.items():
        if "{first_id}" in path:
            if item_id is None:
                continue
            path = path.format(first_id=item_id)
        pages[name] = path
    if args.page:
        pages = {k: v for k, v in pages.items() if v == args.page}
        if not pages:
            pages = {"custom": args.page}

    vps = {k: v for k, v in VIEWPORTS.items()
           if args.width is None or v[0] == args.width}
    if not vps:
        print(f"没有宽度为 {args.width} 的预设,用 --list 看可用项", file=sys.stderr)
        return 1

    results = []
    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--no-sandbox", "--disable-dev-shm-usage"])
        for vname, (w, h, desc) in vps.items():
            ctx = browser.new_context(
                viewport={"width": w, "height": h},
                device_scale_factor=2,
                color_scheme="dark" if args.dark else "light",
                is_mobile=w < 700,
                has_touch=w < 700,
                user_agent=("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
                            "AppleWebKit/605.1.15 (KHTML, like Gecko) "
                            "Version/17.0 Mobile/15E148 Safari/604.1") if w < 700 else None,
            )
            page = ctx.new_page()
            for pname, path in pages.items():
                url = f"{url_root}{path}"
                if t:
                    url += ("&" if "?" in path else "?") + f"k={t}"
                try:
                    page.goto(url, wait_until="networkidle", timeout=30000)
                except Exception as e:  # noqa: BLE001
                    print(f"  ❌ {vname}/{pname}: {type(e).__name__}: {str(e)[:80]}")
                    continue
                # 用 cookie 记住口令后,后续页面就不必每次带 k 了
                suffix = "_dark" if args.dark else ""
                fp = outdir / f"{vname}_{w}x{h}_{pname}{suffix}.png"
                page.screenshot(path=str(fp), full_page=args.full)
                results.append((vname, w, h, pname, fp))
                print(f"  ✅ {vname:9s} {w:>4}x{h:<5} {pname:10s} -> {fp}")
            ctx.close()
        browser.close()

    print(f"\n共 {len(results)} 张,输出目录 {outdir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
