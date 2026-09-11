"""期刊等级标签的渲染规则。

easyScholar 一口气返回十几个体系(SCI 分区、中科院分区、影响因子、北核、
CSCD、ESI、各高校自己的分级……)。全铺在卡片上会淹掉真正重要的信息,
所以这里做两件事:**只留关心的字段**,**把长名字压成短标签**。

配置写在 config.yaml 的 ``journal_rank`` 下:

    journal_rank:
      enabled: true
      fields: [sciwarn, sci, sciUp, sciif, pku, cssci]
      map:
        北大中文核心: 北核          # 字段名 → 想要的短标签
        SCI: ""                    # 空字符串 = 不显示标签,只留值
        "/生物学(\\d+)区/": "生$1"   # /正则/ 作用于【值】,$1 是捕获组

规则分两类,各管各的,互不干扰:
  · 字面量键 —— 匹配**字段的显示名**,决定这个标签叫什么(空 = 不显示标签)。
  · ``/正则/``  键 —— 匹配**字段的值**,决定值怎么缩写(化学1区 → 化1)。

为什么空标签不等于"隐藏该字段":用户列出的 6 个字段里有一半在 map 里是空的,
如果把空当成隐藏,那他自己要的字段就全没了。空只表示"别印名字,只印值"。
"""
from __future__ import annotations

import re
from typing import Any

# 字段 → 默认显示名。map 里没有对应字面量规则时用它。
FIELD_NAMES: dict[str, str] = {
    "sci": "SCI",
    "sciif": "SCIIF",
    "sciif5": "SCIIF(5)",
    "sciUp": "SCI升级版",
    "sciBase": "SCI基础版",
    "sciwarn": "SCIWARN",
    "sciWarning": "SCIWARN",
    "pku": "北大中文核心",
    "cssci": "CSSCI",
    "cscd": "CSCD",
    "eii": "EI检索",
    "ei": "EI检索",
    "esi": "ESI",
    "jci": "JCI",
    "ahci": "AHCI",
    "ssci": "SSCI",
    "zhongguokejihexin": "中国科技核心",
}

# 值是"是/否"这类开关时,只印标签就够了("北大中文核心 是" 很蠢)
_YES = {"是", "y", "yes", "true", "1", "√"}

_REGEX_KEY = re.compile(r"^/(.*)/$", re.S)
# 用户写 $1,Python 的 re.sub 要 \g<1>。不翻译的话 "/化学(\d+)区/=化$1"
# 会原样吐出 "化$1" —— 实测踩到过。
_DOLLAR = re.compile(r"\$(\d+)")


class Renderer:
    """把 ``{字段: 值}`` 渲染成一组短标签。规则解析一次,反复用。"""

    def __init__(self, fields: list[str], mapping: dict[str, Any] | None = None):
        self.fields = list(fields or [])
        self.labels: dict[str, str] = {}          # 字段名 → 标签(空串=不显示标签)
        self.rules: list[tuple[re.Pattern[str], str]] = []   # 值上的正则替换
        for key, val in (mapping or {}).items():
            m = _REGEX_KEY.match(str(key).strip())
            if m:
                try:
                    self.rules.append(
                        (re.compile(m.group(1)),
                         _DOLLAR.sub(r"\\g<\1>", str(val)))
                    )
                except re.error:
                    continue          # 用户写错正则不该让页面挂掉
            else:
                self.labels[str(key).strip()] = "" if val is None else str(val)

    def _label(self, field: str) -> str:
        name = FIELD_NAMES.get(field, field)
        return self.labels.get(name, name)

    def _value(self, field: str, value: str) -> str:
        v = value.strip()
        for pat, rep in self.rules:
            new = pat.sub(rep, v)
            if new != v:
                return new.strip()
        return v

    def tags(self, ranks: dict[str, str] | None) -> list[str]:
        out: list[str] = []
        for f in self.fields:
            raw = (ranks or {}).get(f)
            if not raw:
                continue
            label = self._label(f)
            val = self._value(f, str(raw))
            if val.lower() in _YES:
                tag = label
            elif not label or label == val:
                tag = val or label
            else:
                tag = f"{label} {val}"
            if tag and tag not in out:
                out.append(tag)
        return out


# 用户给的默认映射,原样落成 config.example.yaml 里的那一份
DEFAULT_MAP: dict[str, str] = {
    "北大中文核心": "北核",
    "SCI": "",
    "SCIIF": "",
    "SCIIF(5)": "",
    "CSCD": "",
    "EI检索": "EI",
    "SCI升级版": "",
    "SCIWARN": "",
    "/生物学(\\d+)区/": "生$1",
    "/农林科学(\\d+)区/": "农$1",
    "/环境科学与生态学(\\d+)区/": "环$1",
    "/工程技术(\\d+)区/": "工$1",
    "/地球科学(\\d+)区/": "地$1",
    "/计算机科学(\\d+)区/": "计$1",
    "/人文科学(\\d+)区/": "人$1",
    "/综合性期刊(\\d+)区/": "综$1",
    "/社会学(\\d+)区/": "社$1",
    "/材料科学(\\d+)区/": "材$1",
    "/医学(\\d+)区/": "医$1",
    "/教育学(\\d+)区/": "教育$1",
    "/化学(\\d+)区/": "化$1",
    "/物理与天体物理(\\d+)区/": "物$1",
}

DEFAULT_FIELDS = ["sciwarn", "sci", "sciUp", "sciif", "pku", "cssci"]
