"""管理员密码的哈希与校验 —— 只用标准库,不引入额外依赖。

为什么除了 ``LITRADAR_TOKEN`` 还要一道密码:``/admin/run/*`` 里的
rank / summarize 会真的花掉 DeepSeek 额度,而 token 是放在 URL 里的**长期**
凭据 —— 一旦从浏览器历史、反代日志或 Referer 泄露,拿到它的人就能直接烧钱。
给花钱的阶段再要一次只在内存里存在、用完即忘的密码,可以把"看到过链接"和
"能花钱"分开。

**这是步进验证(sudo 模式),不是 2FA。** 两个凭据都是"你知道的东西";
真正的第二因子需要 TOTP 之类"你有的东西"。这里刻意不假装是 2FA。

存储用 PBKDF2-HMAC-SHA256 而不是 scrypt:后者在某些构建/平台上受内存上限
影响(``hashlib.scrypt`` 会直接抛异常),PBKDF2 到处都有且行为可预期。对单用户
LAN 工具,足够。

存储格式(一整行写进 .env)::

    pbkdf2_sha256$<迭代次数>$<salt base64>$<hash base64>
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import os

ALGORITHM = "pbkdf2_sha256"

# OWASP 对 PBKDF2-HMAC-SHA256 的建议量级。校验一次约几十毫秒 —— 对"点一下
# 按钮"无感,但让离线爆破的成本高得多。
ITERATIONS = 600_000
SALT_BYTES = 16

# 从存储串里读出的迭代次数也要设上限:一个手滑多写几个零的 .env 会让每次
# 校验变成几十秒的 CPU 占用,等于自造 DoS。
MAX_ITERATIONS = 5_000_000

MIN_PASSWORD_LENGTH = 8


def hash_password(password: str, *, iterations: int = ITERATIONS) -> str:
    """生成可写进 .env 的哈希串。"""
    if not password:
        raise ValueError("密码不能为空")
    salt = os.urandom(SALT_BYTES)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return "$".join((
        ALGORITHM,
        str(iterations),
        base64.b64encode(salt).decode("ascii"),
        base64.b64encode(digest).decode("ascii"),
    ))


def verify_password(password: str, stored: str | None) -> bool:
    """比对密码。

    格式不合法的存储值一律返回 False,**不抛异常也不放行** —— 坏配置必须
    表现为"打不开",而不是"门没锁"。
    """
    if not password or not stored:
        return False
    parts = stored.split("$")
    if len(parts) != 4 or parts[0] != ALGORITHM:
        return False
    try:
        iterations = int(parts[1])
        salt = base64.b64decode(parts[2], validate=True)
        expected = base64.b64decode(parts[3], validate=True)
    except (ValueError, TypeError, binascii.Error):
        return False
    if not 1 <= iterations <= MAX_ITERATIONS or not salt or not expected:
        return False
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return hmac.compare_digest(digest, expected)
