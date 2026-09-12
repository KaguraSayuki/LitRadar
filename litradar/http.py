"""HTTP 工具:统一 UA、polite pool、重试与退避。"""
from __future__ import annotations

import time
from typing import Any

import requests

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "LitRadar/0.1 (personal literature radar)"})

_last_call: dict[str, float] = {}


def _throttle(host: str, min_interval: float) -> None:
    prev = _last_call.get(host, 0.0)
    wait = min_interval - (time.time() - prev)
    if wait > 0:
        time.sleep(wait)
    _last_call[host] = time.time()


def get_json(url: str, *, params: dict | None = None, min_interval: float = 0.2,
             retries: int = 3, timeout: int = 30) -> Any | None:
    """GET 并解析 JSON。失败返回 None 而不是抛异常,避免单条坏数据中断整批。"""
    host = url.split("/")[2] if "//" in url else url
    last = ""                      # 最后一次失败的状态码或异常类型
    for attempt in range(retries):
        _throttle(host, min_interval)
        try:
            r = SESSION.get(url, params=params, timeout=timeout)
            if r.status_code == 404:
                return None
            if r.status_code == 429 or r.status_code >= 500:
                last = f"HTTP {r.status_code}"
                time.sleep(1.5 * (attempt + 1))
                continue
            r.raise_for_status()
            return r.json()
        except Exception as e:  # noqa: BLE001
            last = f"{type(e).__name__}: {str(e)[:70]}"
            time.sleep(0.8 * (attempt + 1))
    # 重试耗尽:留一行痕迹。以前这里静默返回 None,Crossref 路径上
    # "网络挂了"和"命中 0 条"在日志里长得一模一样(S2 模块自己有告警,这条通用路径没有)。
    print(f"    [warn] {host} 放弃({last or '未知'}): {url}")
    return None
