"""管理员密码哈希:只存哈希、坏配置必须表现为"打不开"。

这条防线保护的是唯一会花钱的接口,所以边界比功能更重要:格式错误、迭代
次数离谱、空值,都必须是"拒绝",不能是"放行"。
"""
from __future__ import annotations

import pytest

from litradar import passwords


def test_round_trip():
    stored = passwords.hash_password("correct horse battery staple")

    assert passwords.verify_password("correct horse battery staple", stored)
    assert not passwords.verify_password("wrong password", stored)


def test_hash_is_salted_and_versioned():
    a = passwords.hash_password("same")
    b = passwords.hash_password("same")

    assert a != b, "同样的密码不能产生同样的哈希(每次都要新 salt)"
    assert a.startswith(passwords.ALGORITHM + "$")
    assert len(a.split("$")) == 4
    assert passwords.verify_password("same", a)
    assert passwords.verify_password("same", b)


def test_unicode_password():
    stored = passwords.hash_password("化学雷达-密码-🔒")

    assert passwords.verify_password("化学雷达-密码-🔒", stored)
    assert not passwords.verify_password("化学雷达-密码", stored)


def test_iterations_are_embedded_so_old_hashes_stay_valid():
    """以后调高默认迭代次数时,老哈希必须仍然能验。"""
    stored = passwords.hash_password("pw", iterations=1000)

    assert "$1000$" in stored
    assert passwords.verify_password("pw", stored)


@pytest.mark.parametrize("stored", [
    None, "", "   ", "not-a-hash", "pbkdf2_sha256$abc$xx$yy",
    "pbkdf2_sha256$1000$notbase64!!$alsobad!!",
    "md5$1000$c2FsdA==$aGFzaA==",            # 算法名不对
    "pbkdf2_sha256$1000$c2FsdA==",           # 段数不够
    "pbkdf2_sha256$0$c2FsdA==$aGFzaA==",     # 迭代 0
    "pbkdf2_sha256$-5$c2FsdA==$aGFzaA==",    # 负迭代
    "pbkdf2_sha256$999999999999$c2FsdA==$aGFzaA==",   # 迭代离谱 -> 防自造 DoS
    "pbkdf2_sha256$100$" + "$",              # 空 salt / 空 hash
])
def test_malformed_stored_hash_never_verifies(stored):
    assert passwords.verify_password("anything", stored) is False


def test_empty_password_never_verifies():
    stored = passwords.hash_password("real")

    assert passwords.verify_password("", stored) is False


def test_hash_password_rejects_empty_input():
    with pytest.raises(ValueError):
        passwords.hash_password("")
