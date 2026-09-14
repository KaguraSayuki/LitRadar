"""interests.yaml 的分组形态:老格式无损升级、slug 稳定、校验到位。

三条不能破:
  1. **老格式(没有 groups:)必须解析成 slug 为 ``default`` 的单个组** —— 库里
     升级后的历史数据都在这个 slug 下,换名字会让收件箱直接变空;
  2. slug 稳定:改名不改身份,显式写了就用显式的;
  3. 校验与加载共用同一套 slug 派生规则,否则会出现"校验说没问题、加载却撞车"。
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from litradar import rank
from litradar.config import Config

LEGACY = {
    "direction": "示例方向 A",
    "search_queries": ['"Sample detection" optical sample'],
    "s2_queries": ['"Sample detection" + sample'],
    "keywords": {"core": ["Optical Sensor"], "bonus": ["selective"]},
    "journals": {"core": ["Organic Letters"], "ok": ["Tetrahedron Letters"]},
    "seed_dois": ["10.5555/litradar.example.006"],
}


def _cfg(data) -> Config:
    cfg = Config()
    cfg.interests_data = data
    return cfg


# ─────────────────────────────────────────── 老格式:必须落到 default 组
def test_老格式解析成_default_单组():
    groups = rank.load_groups(_cfg(LEGACY))

    assert len(groups) == 1
    assert groups[0].slug == "default"
    assert groups[0].name                      # 有个可读名字,便于界面显示
    assert groups[0].enabled and groups[0].llm_rank
    # 字段照旧解析,不做任何搬运
    assert groups[0].core == ["optical sensor"]
    assert groups[0].queries == ['"Sample detection" optical sample']
    assert groups[0].journals_core == ["Organic Letters"]


def test_老格式没有_name_时不会变成空名字():
    data = {k: v for k, v in LEGACY.items()}
    assert rank.load_groups(_cfg(data))[0].name


def test_load_interests_仍然返回第一组():
    """单组调用点(rank / summarize / pipeline)不用改。"""
    assert rank.load_interests(_cfg(LEGACY)).slug == "default"
    assert rank.load_interests(_cfg({"groups": [
        {"slug": "a", "name": "A"}, {"slug": "b", "name": "B"}]})).slug == "a"


def test_空配置也能得到一组():
    assert rank.load_groups(_cfg({}))[0].slug == "default"


# ─────────────────────────────────────────────────── 新格式:多组
def test_groups_解析出多组与各自开关():
    groups = rank.load_groups(_cfg({"groups": [
        {"slug": "org", "name": "有机", "direction": "d1",
         "search_queries": ["a"], "keywords": {"core": ["sensor"]}},
        {"name": "Materials Chemistry", "llm_rank": False, "enabled": False,
         "search_queries": ["b"]},
        {"slug": "cat", "name": "催化", "seed_dois": ["10.1/x"]},
    ]}))

    assert [g.slug for g in groups] == ["org", "materials-chemistry", "cat"]
    assert [g.enabled for g in groups] == [True, False, True]
    assert [g.llm_rank for g in groups] == [True, False, True]
    assert groups[0].core == ["sensor"]
    assert groups[2].seed_dois == ["10.1/x"]


def test_每组有自己的检索词与关键词():
    """这正是分组的意义:两组的检索式与打分词互不干扰。"""
    groups = rank.load_groups(_cfg({"groups": [
        {"slug": "a", "name": "A", "search_queries": ["qa"], "keywords": {"core": ["ka"]}},
        {"slug": "b", "name": "B", "search_queries": ["qb"], "keywords": {"core": ["kb"]}},
    ]}))

    assert groups[0].queries == ["qa"] and groups[1].queries == ["qb"]
    assert groups[0].core == ["ka"] and groups[1].core == ["kb"]


def test_enabled_false_的组被排除在流程外():
    cfg = _cfg({"groups": [
        {"slug": "on", "name": "On"},
        {"slug": "off", "name": "Off", "enabled": False},
    ]})

    assert [g.slug for g in rank.load_enabled_groups(cfg)] == ["on"]


# ─────────────────────────────────────────────────── slug 派生与稳定性
def test_显式_slug_优先():
    groups = rank.load_groups(_cfg({"groups": [
        {"slug": "my-own-slug", "name": "随便什么名字"}]}))

    assert groups[0].slug == "my-own-slug"


def test_纯中文组名派生稳定_slug():
    """中文派生不出 ASCII 时用名字摘要 —— 同名字必须得到同 slug。"""
    first = rank.load_groups(_cfg({"groups": [{"name": "催化化学"}]}))[0].slug
    second = rank.load_groups(_cfg({"groups": [{"name": "催化化学"}]}))[0].slug

    assert first == second and first.startswith("g")
    assert first != rank.load_groups(_cfg({"groups": [{"name": "材料化学"}]}))[0].slug


def test_改名不改_slug_的前提是显式写_slug():
    explicit = rank.load_groups(_cfg({"groups": [
        {"slug": "keep-me", "name": "老名字"}]}))[0].slug
    renamed = rank.load_groups(_cfg({"groups": [
        {"slug": "keep-me", "name": "新名字"}]}))[0].slug

    assert explicit == renamed == "keep-me"


@pytest.mark.parametrize("slug", ["材料", "mat&chem", "mat+chem", "mat%26chem",
                                  "mat/chem?#", "材" * 85 + "a"])
def test_可编码的显式slug校验和加载保持一致(slug):
    data = {"groups": [{"slug": slug, "name": "方向"}]}
    assert rank.validate_interests(data) == []
    assert rank.load_groups(_cfg(data))[0].slug == slug


@pytest.mark.parametrize("slug,needle", [("bad\nslug", "控制字符"), ("\t", "控制字符"),
                                        ("bad\x7fslug", "控制字符"), ("\ud800", "Unicode"),
                                        ("材" * 86, "256"), ("a" * 257, "256")])
def test_无法安全传输的slug在保存和加载时一致拒绝(slug, needle):
    data = {"groups": [{"slug": slug, "name": "Bad"}]}
    errors = rank.validate_interests(data)
    assert any(needle in e for e in errors)
    assert all(e.encode("utf-8") for e in errors)
    with pytest.raises(ValueError, match=needle):
        rank.load_groups(_cfg(data))


# ─────────────────────────────────────────────────────────────── 校验
def test_合法形态都通过():
    assert rank.validate_interests(LEGACY) == []
    assert rank.validate_interests({"groups": [
        {"slug": "a", "name": "A", "search_queries": ["q"]}]}) == []


def test_重复_slug_被拒绝():
    errors = rank.validate_interests({"groups": [
        {"slug": "same", "name": "A"}, {"slug": "same", "name": "B"}]})

    assert len(errors) == 1 and "重复" in errors[0]


def test_混写单方向字段被拒绝():
    """groups: 与单方向字段同层出现时,无法判断哪个生效。"""
    errors = rank.validate_interests({"direction": "d", "groups": [{"name": "A"}]})

    assert len(errors) == 1 and "不要再在同一层写单方向字段" in errors[0]


def test_嵌套_groups_被拒绝():
    errors = rank.validate_interests({"groups": [{"name": "A", "groups": []}]})

    assert "不能再嵌套" in errors[0]


@pytest.mark.parametrize("data,needle", [
    ({"groups": {"a": 1}}, "必须是列表"),
    ({"groups": [42]}, "必须是映射"),
    ({"groups": [{"name": "A", "llm_rank": "yes"}]}, "必须是布尔值"),
    ({"groups": [{"name": "A", "slug": 7}]}, "slug 必须是字符串"),
    ({"groups": [{"keywords": ["x"]}]}, "keywords 必须是映射"),
])
def test_坏形态报出可操作的错(data, needle):
    errors = rank.validate_interests(data)

    assert errors and any(needle in e for e in errors), errors


def test_报错信息能定位到是哪一组():
    errors = rank.validate_interests({"groups": [
        {"slug": "ok", "name": "OK"},
        {"slug": "bad", "name": "Bad", "keywords": ["x"]}]})

    assert any(e.startswith("groups[1](bad)") for e in errors), errors


def test_web_保存时会要求_groups_非空(tmp_path):
    """require_keys=True 是网页保存路径:空列表等于把方向全删了。"""
    errors = rank.validate_interests({"groups": []})

    assert errors and "不能是空列表" in errors[0]
    # 而加载路径(require_keys=False)不该因为空列表就报错
    assert rank.validate_interests({"groups": []}, require_keys=False) == []


def test_校验与加载的_slug_派生一致():
    """两处规则一旦分叉,就会出现"校验通过、加载却撞 slug"。"""
    data = {"groups": [
        {"name": "Materials Chemistry"},
        {"name": "材料化学"},
        {"slug": "explicit", "name": "X"},
    ]}
    errors = rank.validate_interests(data)
    slugs = [g.slug for g in rank.load_groups(_cfg(data))]

    assert errors == []
    assert len(set(slugs)) == 3
    assert slugs[2] == "explicit"


# ───────────────────────────────────────────────── 与示例文件保持一致
def test_示例文件仍是单方向格式且能加载():
    """示例保持单方向:用户直接复制就能用,分组是可选升级。"""
    path = Path(__file__).resolve().parents[1] / "interests.example.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))

    assert "groups" not in data
    assert rank.validate_interests(data) == []
    assert rank.load_groups(_cfg(data))[0].slug == "default"


def test_真实使用的_interests_文件能加载(tmp_path):
    """把 legacy 写进文件再经 config 读一遍,确认端到端一致。"""
    path = tmp_path / "interests.yaml"
    path.write_text(yaml.safe_dump(LEGACY, allow_unicode=True), encoding="utf-8")
    cfg = Config()
    cfg.interests_data = yaml.safe_load(path.read_text(encoding="utf-8"))

    groups = rank.load_groups(cfg)

    assert len(groups) == 1 and groups[0].slug == "default"
    assert groups[0].journals_ok == ["Tetrahedron Letters"]


def test_公开模板不预设个人研究偏好():
    path = Path(__file__).resolve().parents[1] / "interests.example.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    for field in ("search_queries", "s2_queries", "seed_dois", "authors_watch", "negative"):
        assert data[field] == [], field
    assert all(not values for values in data["keywords"].values())
    assert all(not values for values in data["journals"].values())
