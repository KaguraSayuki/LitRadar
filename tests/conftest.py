"""测试的进程级隔离:不让开发机上的真实 .env 漏进测试。

``litradar.config`` 在**导入时**就读项目根的 ``.env`` 并写进 ``os.environ``。
于是开发机上真实存在的密钥会变成测试的隐含前提 —— 实测踩过这个坑:一旦在
``.env`` 里设了 ``LITRADAR_ADMIN_PASSWORD_HASH``(用 ``litradar admin-password``
设的),``/admin/run/*`` 的 17 个用例会全部变成 401,而那些用例本身完全正确。

这里把两条路都堵上:

1. 导入后清掉 ``.env`` 可能注入的密钥;
2. 把 ``load_env_file`` 换成空操作 —— ``webapp.get_cfg()`` 之类的调用点会在
   每次请求时重读 ``.env``,不堵这里清掉的值又会被读回来。

需要某个变量的用例自己 ``monkeypatch.setenv``,这样每个用例的前提都是显式的。
"""
from __future__ import annotations

import os

import pytest

# .env.example 里出现过的密钥名,外加几个历史 / 可选的名字。
_SECRET_ENV_KEYS = (
    "DEEPSEEK_API_KEY",
    "S2_API_KEY",
    "EASYSCHOLAR_SECRET_KEY",
    "IMAP_PASSWORD",
    "LITRADAR_TOKEN",
    "LITRADAR_ADMIN_PASSWORD_HASH",
    "OPENALEX_API_KEY",
    "OUTLOOK_CLIENT_ID",
    "EPO_KEY",
    "EPO_SECRET",
)


@pytest.fixture(autouse=True, scope="session")
def _hermetic_secrets():
    import litradar.config as config

    for key in _SECRET_ENV_KEYS:
        os.environ.pop(key, None)

    original = config.load_env_file
    config.load_env_file = lambda *a, **kw: None      # 别再读回开发机的 .env
    try:
        yield
    finally:
        config.load_env_file = original
