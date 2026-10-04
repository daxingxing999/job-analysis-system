"""岗位标题 -> 工种族 的分类器。

为什么单独写一个
----------------
旧实现（``smart_crawl.family_of_title``）按 ``FAMILY_MATCH_ORDER`` 逐族做
``term in title`` 的子串匹配，有两个结构性毛病：

1. **长短词不分**：``"java" in "javascript前端开发"`` 成立，于是前端岗被判成后端；
   ``"go" in "django"``、``"ui" in "build"`` 同理。
2. **泛化词吃掉具体词**：backend 排在匹配顺序第一位，而它的词表里有「开发」，
   于是「测试开发工程师」「移动端开发工程师」「嵌入式开发工程师」全归 backend。

而这个分类结果不只用于报表——它还喂给 ``city_family_covered`` 与
``sim_city_families``，决定「哪个城市×族已经覆盖过、可以跳过」。
分类错 → 该抓的组合被永久跳过。

现在的规则
----------
1. 命中词按**长度降序**匹配（长词优先），且英文/数字词要求词边界；
2. 具体族优先于泛化族：命中 ``FAMILY_WEAK_TERMS`` 的词只有在没有
   其它非泛化命中时才生效；
3. 同长度时按 ``FAMILY_MATCH_ORDER`` 决定归属。

本模块**只用标准库**。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache

from jobanal.taxonomy import (
    FAMILY_MATCH_ORDER,
    FAMILY_MATCH_TERMS,
    FAMILY_WEAK_TERMS,
    validate_taxonomy,
)

_ASCII_TERM = re.compile(r"^[a-z0-9+#.]+$")


def _term_pattern(term: str) -> re.Pattern[str]:
    """英文/数字词要求词边界；中文词按原样子串匹配。"""
    term = term.lower()
    if _ASCII_TERM.match(term):
        return re.compile(rf"(?<![a-z0-9]){re.escape(term)}(?![a-z0-9])")
    return re.compile(re.escape(term))


@lru_cache(maxsize=4096)
def _pattern(term: str) -> re.Pattern[str]:
    return _term_pattern(term)


@dataclass(frozen=True)
class Match:
    term: str
    family: str
    weak: bool

    def __str__(self) -> str:  # pragma: no cover - 仅用于调试输出
        return f"{self.term}->{self.family}{'(弱)' if self.weak else ''}"


def all_matches(title: str) -> list[Match]:
    """返回标题命中的全部词，按（是否泛化词、词长降序、族优先级）排序。"""
    text = str(title or "").lower()
    if not text:
        return []
    priority = {fid: i for i, fid in enumerate(FAMILY_MATCH_ORDER)}
    hits: list[Match] = []
    for family, terms in FAMILY_MATCH_TERMS.items():
        for term in terms:
            if _pattern(term).search(text):
                hits.append(Match(term=term, family=family, weak=term in FAMILY_WEAK_TERMS))
    hits.sort(key=lambda m: (m.weak, -len(m.term), priority.get(m.family, 99)))
    return hits


def family_of_title_v2(title: str) -> str:
    """把岗位标题归到一个工种族 id；无法判断时返回 ``"other"``。"""
    hits = all_matches(title)
    if not hits:
        return "other"
    return hits[0].family


def match_detail(title: str) -> str:
    """给校准工具用的可读命中说明。"""
    hits = all_matches(title)
    if not hits:
        return ""
    return "、".join(str(hit) for hit in hits[:4])


def family_name(family_id: str) -> str:
    from jobanal.taxonomy import FAMILY_NAME

    return FAMILY_NAME.get(family_id, "未归类")


def taxonomy_problems() -> list[str]:
    return validate_taxonomy()
