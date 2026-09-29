#!/usr/bin/env python3
"""smart_crawl.py — 覆盖度驱动的智能抓取调度器（城市 × 工种族）

为什么需要它
------------
原来的 batch_crawl.py 用的是「固定对角线排列 + 跑过就跳过」：
  1. 城市表只有 8 个，关键词 15 个 —— 组合空间小，几轮就跑完了，
     于是同一批大城市被反复抓（长沙 602 条、西安 317 条，而 210 座城市 0 条）；
  2. 关键词之间高度同义（Python / Java / 后端开发），在同一个城市里换词
     命中同一批岗位，做了大量重复请求；
  3. 组合用没用过只记「跑没跑过」，不记「跑出多少新岗位」，
     没收益的组合下次还会再跑一遍。

本脚本用三件事解决它
--------------------
1. **工种族（family）替代裸关键词**
   21 个工种族覆盖 IT / 产品 / 设计 / 运营 / 市场 / 销售 / 职能 / 教育 /
   医疗 / 制造 / 建筑 / 物流 / 服务业。同一座城市里，一个族只抓一次，
   族内关键词变体按城市轮转，避免同义关键词反复命中同一批岗位。

2. **加权最大最小公平调度（water-filling）+ 冷却约束**
   把「已抓到的岗位数」和「该城市/该族的配额」作差得到缺口，
   缺口越大的城市/族得分越高；再用绝对冷却约束强制「不连着抓同一个城市、
   不连着抓同一个族」。结果就是样本自动从超采的大城市流向 0 数据的城市，
   城市之间、工种之间都更协调。

3. **收益反馈 + 账本（crawl_ledger.json）**
   每个组合抓完都统计「新岗位 / 重复岗位」。零新增的组合标记为 saturated，
   以后不再选；账本完整记录用过的每一个「城市|工种族」组合、当时用的关键词、
   产出条数和时间戳 —— 下次运行自动跳过，不会重复劳动。

只读复用 crawl_and_import 里的路径常量与环境构造，不改动任何已有文件。

常用命令
--------
    py smart_crawl.py --plan                     # 只输出本次调度计划（不抓取）
    py smart_crawl.py --status                   # 查看账本：用过的组合 / 覆盖率
    py smart_crawl.py --report                   # 生成城市×工种族覆盖矩阵报告
    py smart_crawl.py --run --budget 20          # 正式抓取 20 个组合
    py smart_crawl.py --run --strategy breadth   # 先铺广度：每城先来一个族
    py smart_crawl.py --reset                    # 清空账本重来

更新记录
--------
项目变更记录见 docs/变更说明_2026-09-28.md。
"""

from __future__ import annotations

import argparse
import heapq
import json
import os
import random
import subprocess
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

from crawl_and_import import (  # noqa: E402  只读复用，不修改原文件
    ARCHIVE_DIR,
    SCRAPER_SCRIPT,
    build_env,
)

CITY_CODES_FILE = BASE_DIR / "scraper" / "data" / "city_codes.json"
LEDGER_FILE = BASE_DIR / "data" / "crawl_ledger.json"
PLAN_FILE = BASE_DIR / "data" / "crawl_plan_last.json"
REPORT_FILE = BASE_DIR / "data" / "crawl_report.md"
DEFAULT_OUTPUT = BASE_DIR / "data" / "boss_jobs.csv"

LEDGER_VERSION = 2


# ============================================================
# 一、工种族定义：21 个族，覆盖主流与长尾工种
#     weight = 该族在全国岗位池里的体量权重，用于「冷城市 × 热工种」配对，
#              避免拿稀缺请求去撞冷门组合（例如小城市 × 小众职能）。
# ============================================================
JOB_FAMILIES = [
    # id            名称           代表性搜索关键词（按体量排序，城市间轮转使用）                     体量权重
    ("backend",    "后端开发",   ["Java", "Python", "Go", "后端开发", "C++"],                  1.30),
    ("sales",      "销售",       ["销售经理", "销售代表", "大客户销售", "渠道销售"],            1.20),
    ("ai",         "算法/AI",    ["算法工程师", "机器学习", "深度学习", "大模型算法"],          1.15),
    ("frontend",   "前端开发",   ["前端开发", "Web前端", "Vue", "React"],                      1.00),
    ("operation",  "运营",       ["新媒体运营", "用户运营", "电商运营", "内容运营"],            1.00),
    ("edu",        "教育",       ["教师", "培训讲师", "课程顾问", "幼教"],                      1.00),
    ("service",    "服务业",     ["客服专员", "餐饮店长", "酒店管理", "房产经纪人"],            1.00),
    ("mech",       "机械/电气",  ["机械工程师", "电气工程师", "自动化工程师", "工艺工程师"],    0.95),
    ("product",    "产品",       ["产品经理", "高级产品经理", "硬件产品经理"],                  0.95),
    ("test",       "测试",       ["测试工程师", "自动化测试", "软件测试"],                      0.85),
    ("data",       "数据",       ["数据分析师", "数据开发", "大数据工程师", "BI工程师"],        0.85),
    ("design",     "设计",       ["UI设计师", "视觉设计师", "交互设计师", "平面设计"],          0.85),
    ("marketing",  "市场",       ["市场营销", "品牌营销", "市场推广", "广告投放"],              0.85),
    ("finance",    "财务/会计",  ["财务会计", "会计", "出纳", "财务经理"],                      0.85),
    ("medical",    "医疗健康",   ["护士", "医师", "药师", "医药代表"],                          0.85),
    ("logistics",  "物流/供应链", ["物流专员", "采购专员", "仓储主管", "供应链管理"],            0.85),
    ("hr",         "人力/行政",  ["人力资源", "招聘专员", "行政专员", "人事经理"],              0.80),
    ("ops",        "运维/云",    ["运维工程师", "DevOps", "云计算工程师", "网络工程师"],        0.80),
    ("construct",  "建筑/土木",  ["施工员", "土建工程师", "结构工程师", "工程造价"],            0.80),
    ("security",   "安全",       ["网络安全", "信息安全", "安全工程师"],                        0.45),
]

FAMILY_BY_ID = {fid: {"id": fid, "name": name, "keywords": kws, "weight": weight}
                for fid, name, kws, weight in JOB_FAMILIES}


# 工种族识别词表：只用于「统计已有岗位属于哪个工种」，不参与抓取。
# 比 JOB_FAMILIES 里的搜索关键词宽得多，因为岗位标题是长尾的（实测 3739 种不同标题）。
FAMILY_MATCH_TERMS = {
    "backend":   ["java", "python", "golang", "go开发", "php", "c++", "node", "后端", "服务端",
                  "开发工程师", "软件开发", "研发工程师", "架构师", "开发"],
    "frontend":  ["前端", "web开发", "vue", "react", "html", "小程序", "h5", "页面"],
    "mobile":    ["android", "ios", "移动端", "flutter", "客户端开发", "安卓"],
    "ai":        ["算法", "机器学习", "深度学习", "大模型", "人工智能", "nlp", "计算机视觉",
                  "推荐算法", "数据挖掘", "ai工程", "智能"],
    "test":      ["测试", "qa", "质量保证"],
    "ops":       ["运维", "devops", "云计算", "系统工程师", "网络工程师", "实施工程师",
                  "it支持", "sre", "通信", "技术支持"],
    "data":      ["数据分析", "数据开发", "大数据", "数仓", "bi", "etl", "数据工程", "商业分析"],
    "security":  ["安全", "渗透", "等保", "风控"],
    "product":   ["产品经理", "产品专员", "产品助理"],
    "design":    ["设计师", "美工", "视觉", "ui", "交互", "平面", "设计"],
    "operation": ["运营"],
    "marketing": ["市场", "营销", "推广", "策划", "投放", "seo", "sem", "品牌", "公关"],
    "sales":     ["销售", "业务员", "客户经理", "招商", "电销", "bd", "拓展", "渠道", "顾问式"],
    "hr":        ["人力", "人事", "招聘", "行政", "hr", "薪酬", "培训专员"],
    "finance":   ["会计", "财务", "出纳", "审计", "税务", "资金", "结算", "收银", "统计"],
    "edu":       ["教师", "老师", "讲师", "教研", "教务", "助教", "培训", "课程顾问",
                  "辅导", "幼教", "教练", "保育"],
    "medical":   ["护士", "护理", "医师", "医生", "药师", "药店", "医药", "临床", "口腔",
                  "康复", "检验", "理疗", "兽医"],
    "mech":      ["机械", "电气", "自动化", "工艺", "模具", "数控", "cnc", "设备工程师",
                  "结构设计", "机电", "质检员", "普工", "操作工", "技工", "生产", "焊接"],
    "construct": ["施工", "土建", "结构工程师", "造价", "监理", "测量", "工程管理",
                  "预算员", "建筑", "安装工程", "暖通", "水电"],
    "logistics": ["物流", "采购", "仓储", "仓管", "供应链", "跟单", "报关", "货运",
                  "调度", "配送", "装卸", "库存"],
    "service":   ["客服", "服务员", "店员", "导购", "前台", "店长", "餐饮", "酒店", "保洁",
                  "保安", "美容", "房产经纪人", "物业", "快递", "保姆", "月嫂", "司机",
                  "饮品", "收银员"],
}
# 匹配顺序：越靠前越优先。把体量大、特征强的族放前面，避免「医疗器械销售」被误判成医疗。
FAMILY_MATCH_ORDER = [
    "backend", "frontend", "mobile", "ai", "data", "test", "ops", "security", "design",
    "product", "operation", "marketing", "sales", "medical", "edu", "hr", "finance",
    "mech", "construct", "logistics", "service",
]


# ============================================================
# 二、城市分档：给每座城市一个体量权重（配额用）
#     未列出的城市按「其他」档处理，权重 1。可自行增删。
# ============================================================
CITY_TIERS = {
    "一线": (["北京", "上海", "广州", "深圳"], 8.0),
    "新一线": (["成都", "杭州", "重庆", "西安", "苏州", "武汉", "南京", "天津", "郑州",
                "长沙", "东莞", "佛山", "合肥", "青岛", "沈阳", "宁波", "昆明", "无锡"], 4.0),
    "二线": (["厦门", "福州", "济南", "大连", "哈尔滨", "温州", "长春", "石家庄", "泉州",
              "南宁", "贵阳", "南昌", "金华", "常州", "南通", "嘉兴", "太原", "徐州",
              "保定", "珠海", "中山", "惠州", "烟台", "兰州", "海口", "乌鲁木齐",
              "呼和浩特", "银川", "西宁", "拉萨", "绍兴", "台州", "扬州", "潍坊",
              "临沂", "洛阳", "唐山", "襄樊", "襄阳", "宜昌", "盐城", "泰州", "镇江",
              "淄博", "济宁", "廊坊", "汕头", "湛江", "桂林", "柳州", "遵义"], 2.0),
}
DEFAULT_CITY_WEIGHT = 1.0

# —— 「全国」是个特殊的搜索范围，不是城市 ——
# 实测：1 次请求（约 2 分钟）单城模式只覆盖 1 座城市，
# 而「全国」模式能拿到同时分散在 28 座城市的岗位。
# 所以它在「铺广度」阶段的性价比是单城模式的二十多倍，必须单独加权，
# 而且不能参与城市配额的分母（否则会把真实城市的配额摊薄）。
NATIONWIDE = "全国"
NATIONWIDE_WEIGHT = 12.0
# 不同策略下「全国」的优先倍数：铺广度时让它霸榜，均衡时适度，深耕时完全不碰
NATIONWIDE_FACTOR = {"balanced": 2.0, "breadth": 3.5, "depth": 0.0}

# 这些是行政区划上的「市/群岛」，但 BOSS 上几乎没有在招岗位，
# 抓了基本是空跑，默认排除。用 --include-all-cities 可以放回来。
DEFAULT_EXCLUDE_CITIES = {"三沙", "东沙群岛"}

# —— 维度切片：突破单查询 150 条硬上限 ——
# BOSS 单次搜索最多 10 页 × 15 条 = 150 条。实测 13 个批次全部是
# 75 条（5 页）或 45 条（3 页），一次都没提前跑完 → 查询被上限截断了。
# 用经验/学历筛选把同一批搜索切成若干互斥子集，每个子集各自 150 条，
# 单组合产能可以从 150 抬到约 1000+，且不用改任何已有代码。
# 代码见 boss_cdp_raw.py 的 --experience / --degree 说明。
EXPERIENCE_SLICES = [
    ("exp108", "在校/应届", ["--experience", "108"]),
    ("exp103", "1年以内",  ["--experience", "103"]),
    ("exp104", "1-3年",   ["--experience", "104"]),
    ("exp105", "3-5年",   ["--experience", "105"]),
    ("exp106", "5-10年",  ["--experience", "106"]),
    ("exp107", "10年以上", ["--experience", "107"]),
]
DEGREE_SLICES = [
    ("deg209", "初中及以下", ["--degree", "209"]),
    ("deg208", "中专/中技", ["--degree", "208"]),
    ("deg206", "高中",     ["--degree", "206"]),
    ("deg202", "大专",     ["--degree", "202"]),
    ("deg203", "本科",     ["--degree", "203"]),
    ("deg204", "硕士",     ["--degree", "204"]),
    ("deg205", "博士",     ["--degree", "205"]),
]
SLICE_PRESETS = {"experience": EXPERIENCE_SLICES, "degree": DEGREE_SLICES}
SLICE_LABEL = {sid: label for sid, label, _ in EXPERIENCE_SLICES + DEGREE_SLICES}


def city_weight(city: str) -> float:
    if city == NATIONWIDE:
        return NATIONWIDE_WEIGHT
    for _tier, (names, weight) in CITY_TIERS.items():
        if city in names:
            return weight
    return DEFAULT_CITY_WEIGHT


# 展示用名称：搜索参数仍传原始名，但输出一律用规范称谓
CITY_DISPLAY = {"香港": "中国香港", "澳门": "中国澳门", "台湾": "中国台湾"}


def display_city(city: str) -> str:
    return CITY_DISPLAY.get(city, city)


# ============================================================
# 三、策略预设
# ============================================================
STRATEGIES = {
    # balanced：默认。广度与深度兼顾，按缺口水位法自然推进
    "balanced": {"total_target": 25000, "max_families_per_city": 6, "zero_bonus": 1.0},
    # breadth：先把 373 座城市全部铺到「至少有数据」，每城先跑 1-2 个大族
    "breadth": {"total_target": 25000, "max_families_per_city": 2, "zero_bonus": 3.0},
    # depth：忽略 0 数据城市，在已有数据的城市里把工种族铺满
    "depth": {"total_target": 40000, "max_families_per_city": 12, "zero_bonus": 0.5},
}


# ============================================================
# 四、基础 I/O
# ============================================================
def load_city_codes(*, include_nationwide: bool = False) -> dict[str, str]:
    """读取全量城市码表。默认排除「全国」这种伪城市。"""
    raw = json.loads(CITY_CODES_FILE.read_text(encoding="utf-8"))
    if include_nationwide:
        return dict(raw)
    return {name: code for name, code in raw.items() if name != "全国"}


def empty_ledger() -> dict:
    return {
        "version": LEDGER_VERSION,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "updated_at": None,
        "combos": {},          # "城市|族id" -> 记录
        "runs": [],            # 每次执行的摘要
    }


def load_ledger() -> dict:
    if LEDGER_FILE.exists():
        try:
            data = json.loads(LEDGER_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict) and "combos" in data:
                data.setdefault("runs", [])
                return data
        except (OSError, json.JSONDecodeError) as exc:
            print(f"[警告] 账本损坏，已重建：{exc}")
    return empty_ledger()


def save_ledger(ledger: dict) -> None:
    LEDGER_FILE.parent.mkdir(parents=True, exist_ok=True)
    ledger["updated_at"] = datetime.now().isoformat(timespec="seconds")
    tmp = LEDGER_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(ledger, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, LEDGER_FILE)


def combo_key(city: str, family_id: str, slice_id: str = "") -> str:
    """账本键。基础组合是「城市|族」，切片子查询追加一段「|切片id」。

    保持两段式格式不变，这样老账本（13 个组合）依然能被识别。
    """
    return f"{city}|{family_id}" + (f"|{slice_id}" if slice_id else "")


def parse_combo_key(key: str) -> tuple[str, str, str]:
    parts = key.split("|")
    city = parts[0] if len(parts) > 0 else ""
    family_id = parts[1] if len(parts) > 1 else ""
    slice_id = parts[2] if len(parts) > 2 else ""
    return city, family_id, slice_id


def family_key(city: str, family_id: str) -> str:
    """只到「城市|族」两级，用来统计某城已铺多少族、已做多少切片。"""
    return f"{city}|{family_id}"


# ============================================================
# 五、已有数据盘点：从归档里还原「城市 → 岗位数」「族 → 岗位数」
#     这是水位的起点 —— 已经抓过 602 条的城市，缺口自然就小。
# ============================================================
def family_of_title(title: str) -> str:
    """按岗位标题判断所属工种族，用于统计各工种已覆盖情况。

    实测归档里有 3739 种不同标题，所以词表要宽：
    先按搜索关键词精确命中，再按 FAMILY_MATCH_TERMS 的宽词表命中，最后才归入 other。
    """
    text = str(title or "")
    low = text.lower()

    # 一、搜索关键词（精确，优先）
    for fid, _, keywords, _ in JOB_FAMILIES:
        for kw in keywords:
            if kw.lower() in low:
                return fid

    # 二、宽词表（按 FAMILY_MATCH_ORDER 保证优先级）
    for fid in FAMILY_MATCH_ORDER:
        for term in FAMILY_MATCH_TERMS.get(fid, ()):
            if term in low:
                return fid
    return "other"


def scan_archive() -> tuple[set[str], Counter, Counter, Counter]:
    """扫描 抓取结果/ 下全部批次，返回 (已知 job_id 集合, 城市岗位数, 族岗位数, 城市族组合数)。

    「城市族组合数」来自归档里每个岗位的 city + title-族 组合，
    这样即使没有账本（历史数据是别的脚本抓的），也知道哪些城市已经覆盖过哪些工种。
    """
    known_ids: set[str] = set()
    city_counts: Counter = Counter()
    family_counts: Counter = Counter()
    city_family_counts: Counter = Counter()
    files = sorted(ARCHIVE_DIR.rglob("boss_jobs_*.json")) if ARCHIVE_DIR.exists() else []

    for path in files:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        jobs = payload.get("jobs", []) if isinstance(payload, dict) else payload
        if not isinstance(jobs, list):
            continue
        for job in jobs:
            if not isinstance(job, dict):
                continue
            job_id = str(job.get("job_id") or "").strip()
            if job_id:
                if job_id in known_ids:
                    continue
                known_ids.add(job_id)
            city = str(job.get("location") or "").split("·")[0].strip() or "未知"
            fid = family_of_title(job.get("title"))
            city_counts[city] += 1
            family_counts[fid] += 1
            city_family_counts[combo_key(city, fid)] += 1
    return known_ids, city_counts, family_counts, city_family_counts


def ledger_city_family_counts(ledger: dict) -> Counter:
    """账本里已跑成功的组合（用于「同城同族只跑一次」与冷却）。"""
    done = Counter()
    for key, record in ledger.get("combos", {}).items():
        if record.get("status") == "done":
            done[key] = int(record.get("runs", 1))
    return done


# ============================================================
# 六、调度器：加权最大最小公平（water-filling）+ 冷却 + 收益反馈
# ============================================================
class Scheduler:
    """覆盖度驱动的贪心调度器。

    打分公式（全部因子都在 [0, 1] 或为正权重，量纲统一，便于解释）::

        score = city_gap × family_gap × family_volume × cooldown × zero_bonus

        city_gap     = clamp((城市配额 - 城市已有岗位) / 城市配额, 0, 1)
        family_gap   = clamp((族配额   - 族已有岗位  ) / 族配额,   0, 1)
        family_volume= 族的体量权重（冷城市配热工种，避免撞冷门）
        cooldown     = 命中「同城/同族冷却」时的惩罚系数
        zero_bonus   = 该城市一条数据都没有时的加成（breadth 策略下加大）

    两个 gap 相乘 → 只有「城市缺 + 工种也缺」的组合才会得高分，
    这就同时把城市维度和工种维度拉平（协调）。

    效率上不用每轮重排全表：初始把候选压进最大堆，
    每次取堆顶时重算一次分数；分数只会随覆盖度上升而下降，
    所以过期堆项重算一次后重新入堆即可收敛，
    单轮选择实际代价接近 O(log N)，而不是 O(N log N) 全量重排。
    """

    def __init__(self, cities, families, ledger, city_counts, family_counts,
                 city_family_done, *, total_target, max_families_per_city,
                 zero_bonus, cooldown_city, cooldown_family,
                 max_runs_per_combo=1, combo_min_jobs=8, city_family_covered=None,
                 nationwide_factor=1.0, max_slices=0):
        self.cities = list(cities)
        self.families = list(families)
        self.ledger = ledger
        self.city_have = Counter(city_counts)
        self.family_have = Counter(family_counts)
        self.city_family_done = Counter(city_family_done)
        # 归档里某个「城市|族」已有多少条 —— 超过阈值就认为这个组合已经覆盖过
        self.city_family_covered = Counter(city_family_covered or {})
        self.max_families_per_city = max_families_per_city
        self.max_runs_per_combo = max_runs_per_combo
        self.combo_min_jobs = combo_min_jobs
        self.zero_bonus = zero_bonus
        self.cooldown_city = cooldown_city
        self.cooldown_family = cooldown_family
        self.nationwide_factor = nationwide_factor
        self.max_slices = max_slices

        # —— 配额分配：按权重把总目标摊到各城市 / 各工种 ——
        # 「全国」不参与城市配额的分母：它不是一座城市，算进去会把真实城市的配额摊薄。
        real_cities = [c for c in self.cities if c != NATIONWIDE]
        total_city_weight = sum(city_weight(c) for c in real_cities) or 1.0
        total_family_weight = sum(f["weight"] for f in self.families) or 1.0
        self.city_quota = {
            c: max(1.0, total_target * city_weight(c) / total_city_weight) for c in real_cities
        }
        if NATIONWIDE in self.cities:
            self.city_quota[NATIONWIDE] = 1.0    # 占位，打分时走特判，不看缺口
        self.family_quota = {
            f["id"]: max(1.0, total_target * f["weight"] / total_family_weight)
            for f in self.families
        }
        self.family_ids = [f["id"] for f in self.families]

        # 规划期的模拟覆盖度（选中后累加预期产出，让后续分数自然衰减）
        self.sim_city = Counter({c: float(self.city_have.get(c, 0)) for c in self.cities})
        self.sim_family = Counter({f: float(self.family_have.get(f, 0)) for f in self.family_ids})
        self.sim_city_families = defaultdict(int)
        # 账本里某「城市|族」已经做了几个切片子查询（键是三级格式）
        self.slice_counts: Counter = Counter()
        for key, record in ledger.get("combos", {}).items():
            _city, _fid, slice_id = parse_combo_key(key)
            if slice_id and record.get("status") in ("done", "saturated"):
                self.slice_counts[family_key(_city, _fid)] += 1
        for key, runs in self.city_family_done.items():
            city, fid, slice_id = parse_combo_key(key)
            if not slice_id:
                self.sim_city_families[city] += runs
        # 归档里已经覆盖过的「城市|族」也算进该城已铺族数
        for key, count in self.city_family_covered.items():
            if count >= self.combo_min_jobs:
                city, _fid, _sid = parse_combo_key(key)
                self.sim_city_families[city] += 1

        self.planned_city_family: set[str] = set()
        self.recent_cities: list[str] = []
        self.recent_families: list[str] = []
        # 惰性删除用的最大堆：heap 存 (-分数, 序号, 城市, 族)，
        # pushed 记录每个候选「最近一次入堆的分数」，用来识别被取代的旧堆项。
        self.heap: list[tuple] = []
        self.pushed: dict[str, float] = {}
        self.seq = 0
        self.city_rank = {c: i for i, c in enumerate(
            sorted(self.cities, key=lambda c: (-city_weight(c), c)))}
        # 一次组合大致带来的新增岗位数（5 页 × 15 条 × 去重后留存率）
        self.per_combo_credit = 60.0

    # ---------- 硬约束：这些组合一律不再调度 ----------
    def _blocked(self, city: str, fid: str) -> str | None:
        key = family_key(city, fid)
        if key in self.planned_city_family:
            return "本批次已排"
        record = self.ledger.get("combos", {}).get(key)
        slices_done = self.slice_counts.get(key, 0)
        if record:
            status = record.get("status")
            if status == "saturated" and slices_done >= self.max_slices:
                return "历史零新增，已拉黑"
            if status == "done" and int(record.get("runs", 0)) >= self.max_runs_per_combo:
                # 基础查询已抓过。只有「还想补切片」时才允许再次调度。
                if slices_done >= self.max_slices:
                    return "账本记录已抓过"
        covered = self.city_family_covered.get(key, 0)
        if covered >= self.combo_min_jobs and slices_done >= self.max_slices:
            return f"归档已有 {covered} 条"
        if city != NATIONWIDE and self.sim_city_families.get(city, 0) >= self.max_families_per_city:
            return "该城族数已铺满"
        # 「全国」不是一座城市，不受「同城最多铺几族」的约束
        # （它每个族只跑一次，天然受 max_runs_per_combo 限制）
        if city != NATIONWIDE and city not in self.city_have and self.zero_bonus < 0.6:
            return "depth 策略跳过 0 数据城市"  # depth 策略下不碰空白城市
        return None

    # ---------- 打分 ----------
    def market_factor(self, city: str) -> float:
        """市场体量因子：用分档权重的平方根。

        作用是让「同为 0 数据」的大城市排在偏远小城前面 ——
        否则纯缺口水位法会从阿里地区、阿拉尔这类几乎没有岗位的城市开始抓，
        白白浪费一次 2-4 分钟的抓取。权重 1→1.0，2→1.41，4→2.0，8→2.83。
        """
        return city_weight(city) ** 0.5

    def score(self, city: str, fid: str) -> float:
        if self._blocked(city, fid):
            return 0.0
        fam_gap = max(0.0, (self.family_quota[fid] - self.sim_family[fid]) / self.family_quota[fid])
        if fam_gap <= 0:
            return 0.0
        if city == NATIONWIDE:
            # 「全国」没有自己的岗位池（岗位都会落到真实城市里），
            # 所以缺口恒定为满，靠 NATIONWIDE_FACTOR 和族缺口来决定优先级。
            city_gap = 1.0
            if self.nationwide_factor <= 0:
                return 0.0
            value = city_gap * fam_gap * FAMILY_BY_ID[fid]["weight"]
            value *= self.nationwide_factor
            value *= self.market_factor(city) / 3.5   # 归一化，避免双重放大
        else:
            city_gap = max(0.0, (self.city_quota[city] - self.sim_city[city]) / self.city_quota[city])
            if city_gap <= 0:
                return 0.0
            value = city_gap * fam_gap * FAMILY_BY_ID[fid]["weight"]
            value *= self.market_factor(city)        # 大市场优先，别把请求浪费在偏远小城
            if city not in self.city_have:
                value *= self.zero_bonus             # 空白城市优先（广度）
        if city in self.recent_cities[-self.cooldown_city:]:
            value *= 0.12                            # 同城冷却：不连着抓同一座城
        if fid in self.recent_families[-self.cooldown_family:]:
            value *= 0.45                            # 同族冷却：不连着抓同一个工种
        return value

    def reason(self, city: str, fid: str) -> str:
        blocked = self._blocked(city, fid)
        if blocked:
            return blocked
        if city == NATIONWIDE:
            return (f"族缺口{(1 - self.sim_family[fid] / self.family_quota[fid]) * 100:.0f}%"
                    f" 全国铺开(覆盖效率×28)")
        return (f"城缺口{(1 - self.sim_city[city] / self.city_quota[city]) * 100:.0f}%"
                f" 族缺口{(1 - self.sim_family[fid] / self.family_quota[fid]) * 100:.0f}%"
                f" 已有{int(self.city_have.get(city, 0))}条"
                f" 体量×{self.market_factor(city):.2f}")

    def keyword_for(self, city: str, fid: str) -> str:
        """族内关键词按城市轮转 —— 不同城市用不同同义词，扩大覆盖。"""
        keywords = FAMILY_BY_ID[fid]["keywords"]
        return keywords[self.city_rank.get(city, 0) % len(keywords)]

    # ---------- 选组合 ----------
    def _push(self, city: str, fid: str, value: float) -> None:
        key = family_key(city, fid)
        self.pushed[key] = value
        heapq.heappush(self.heap, (-value, self.seq, city, fid))
        self.seq += 1

    def _refresh_one(self, city: str, fid: str) -> None:
        key = family_key(city, fid)
        if key in self.planned_city_family:
            return
        value = self.score(city, fid)
        if value <= 0:
            self.pushed.pop(key, None)
            return
        self._push(city, fid, value)

    def _refresh_after_commit(self, city: str, fid: str) -> None:
        """只重算受这次提交影响的候选，保持堆顶始终是真正的最高分。

        一次提交会改变两组候选的分数：
          1. 刚提交的这座城市（它的城市缺口变了）的全部族；
          2. 刚提交的这个族（族缺口变了）的全部城市；
        外加**冷却滑动窗口**的影响 —— 最近 N 次提交过的城市/族有惩罚，
        而滑出窗口的会恢复原分，所以还要把仍在窗口内的城市/族一并重算。
        每步代价约 O((冷却城市数×族数 + 冷却族数×城市数) × log N)，
        远小于全表 O(N log N) 重排（实测规划 40 个组合约 0.1 秒）。
        """
        hot_cities = set(self.recent_cities[-(self.cooldown_city + 1):]) | {city}
        hot_families = set(self.recent_families[-(self.cooldown_family + 1):]) | {fid}
        for hot_city in hot_cities:
            for other_fid in self.family_ids:
                self._refresh_one(hot_city, other_fid)
        for hot_fid in hot_families:
            for other_city in self.cities:
                self._refresh_one(other_city, hot_fid)

    def plan(self, budget: int, *, guard: int = 400_000) -> list[dict]:
        self.heap: list[tuple] = []
        self.pushed: dict[str, float] = {}
        self.seq = 0
        # 按体量降序入堆：得分相同时先入堆的大城市胜出（seq 小的排前面）
        ordered_cities = sorted(self.cities, key=lambda c: (-city_weight(c), c))
        for city in ordered_cities:
            for fid in self.family_ids:
                value = self.score(city, fid)
                if value > 0:
                    self._push(city, fid, value)

        picks: list[dict] = []
        pops = 0
        while self.heap and len(picks) < budget and pops < guard:
            neg, _, city, fid = heapq.heappop(self.heap)
            pops += 1
            key = family_key(city, fid)
            # 被同键的更新堆项取代过的旧项，直接丢弃（堆是懒删除的）
            if self.pushed.get(key) != -neg:
                continue
            if self._blocked(city, fid):
                self.pushed.pop(key, None)
                continue
            base_record = self.ledger.get("combos", {}).get(key) or {}
            picks.append({
                "city": city,
                "family_id": fid,
                "family_name": FAMILY_BY_ID[fid]["name"],
                "keyword": self.keyword_for(city, fid),
                "score": round(-neg, 6),
                "reason": self.reason(city, fid),
                # 基础查询已经抓过（这次只是来补切片的），跑的时候跳过基础查询
                "skip_base": base_record.get("status") in ("done", "saturated"),
                "slices_done": self.slice_counts.get(key, 0),
            })
            self._commit(city, fid)
            self._refresh_after_commit(city, fid)
        return picks

    def _commit(self, city: str, fid: str) -> None:
        self.planned_city_family.add(family_key(city, fid))
        self.sim_city_families[city] += 1
        self.sim_city[city] += self.per_combo_credit
        self.sim_family[fid] += self.per_combo_credit
        self.recent_cities.append(city)
        self.recent_families.append(fid)


# ============================================================
# 七、抓取执行
# ============================================================
def cdp_alive(port: int) -> bool:
    import urllib.request

    backup = os.environ.get("NO_PROXY", "")
    try:
        os.environ["NO_PROXY"] = "127.0.0.1,localhost"
        urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=3)
        return True
    except Exception:
        return False
    finally:
        os.environ["NO_PROXY"] = backup


def run_command(command: list[str], timeout: int = 1800) -> int:
    print("$ " + " ".join(str(part) for part in command), flush=True)
    try:
        result = subprocess.run(command, cwd=str(BASE_DIR), env=build_env(), timeout=timeout)
        return result.returncode
    except subprocess.TimeoutExpired:
        print(f"  [超时] 超过 {timeout}s", flush=True)
        return -1


def start_browser(python: str, port: int, browser: str) -> bool:
    flag = "--setup-edge" if browser == "edge" else "--setup-chrome"
    print("启动专用浏览器并等待登录（首次使用需要在此浏览器里登录一次 BOSS 直聘）…", flush=True)
    code = run_command(
        [python, str(SCRAPER_SCRIPT), flag, "--cdp-port", str(port), "--login-timeout", "300"]
    )
    if code == 0 and cdp_alive(port):
        return True
    # 浏览器进程随命令退出会消失，只有复用后台实例才能持续抓取
    print("[提示] 浏览器没有保持运行。请用后台方式启动：")
    print(f'  C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe '
          f'--remote-debugging-port={port} '
          f'--user-data-dir=%USERPROFILE%\\.boss-zhipin-scraper\\chrome-profile '
          f'--remote-allow-origins=*')
    return False


def read_job_ids(path: Path) -> tuple[set[str], int]:
    """读取一个抓取结果文件，返回 (job_id 集合, 原始条数)。"""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return set(), 0
    jobs = payload.get("jobs", []) if isinstance(payload, dict) else payload
    if not isinstance(jobs, list):
        return set(), 0
    ids = {str(j.get("job_id")) for j in jobs if isinstance(j, dict) and j.get("job_id")}
    return ids, len(jobs)


def archive_path_for(batch_dir: Path, city: str, fid: str) -> Path:
    """归档文件名必须以 boss_jobs_ 开头，boss_import.py 靠 rglob('boss_jobs_*.json') 识别批次。"""
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return batch_dir / f"boss_jobs_{city}_{fid}_{stamp}.json"


def do_import(python: str, output: str) -> int:
    return run_command([
        python, str(BASE_DIR / "boss_import.py"),
        "--source", str(ARCHIVE_DIR),
        "--output", output,
    ], timeout=600)


# ============================================================
# 八、账本 / 报告输出
# ============================================================
def write_plan_file(picks: list[dict], cfg: dict) -> None:
    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "config": cfg,
        "combos": picks,
    }
    PLAN_FILE.parent.mkdir(parents=True, exist_ok=True)
    PLAN_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def print_plan(picks: list[dict], limit: int = 30) -> None:
    if not picks:
        print("没有可调度的组合了：所有城市/工种的缺口都已填平，或者约束把它们全部挡掉了。")
        return
    print(f"{'#':>3}  {'★':>6}  {'城市':<10} {'工种族':<12} {'关键词':<12} 依据")
    print("-" * 96)
    for index, pick in enumerate(picks[:limit], 1):
        print(f"{index:>3}  {pick['score']:.4f}  {display_city(pick['city']):<12} "
              f"{pick['family_name']:<12} {pick['keyword']:<12} {pick['reason']}")
    if len(picks) > limit:
        print(f"... 还有 {len(picks) - limit} 个组合，完整清单见 {PLAN_FILE.name}")


def print_status(ledger: dict, known_ids: set[str], city_counts: Counter,
                 family_counts: Counter, city_count: int, strategy: str) -> None:
    print("=" * 72)
    print("  抓取账本（smart_crawl）")
    print("=" * 72)
    print(f"账本文件   : {LEDGER_FILE}")
    print(f"账本版本   : v{ledger.get('version')}   最后更新: {ledger.get('updated_at') or '—'}")
    runs = ledger.get("runs", [])
    print(f"执行批次   : {len(runs)} 次")

    combos = ledger.get("combos", {})
    done = {k: v for k, v in combos.items() if v.get("status") == "done"}
    sat = {k: v for k, v in combos.items() if v.get("status") == "saturated"}
    failed = {k: v for k, v in combos.items() if v.get("status") == "failed"}
    print(f"已用组合   : {len(done)} 个（有效）/ {len(sat)} 个（零新增，已拉黑）"
          f" / {len(failed)} 个（失败待重试）")

    if done:
        print("\n最近 15 个已用组合：")
        recent = sorted(done.items(), key=lambda kv: kv[1].get("run_at") or "", reverse=True)[:15]
        print(f"  {'城市':<10} {'工种族':<12} {'关键词':<12} {'切片':<10} {'抓取':>5} {'新增':>5}")
        for key, record in recent:
            city, fid, slice_id = parse_combo_key(key)
            name = FAMILY_BY_ID.get(fid, {}).get("name", fid)
            print(f"  {display_city(city):<12} {name:<12} {str(record.get('keyword', '')):<12} "
                  f"{SLICE_LABEL.get(slice_id, slice_id or '—'):<10} "
                  f"{record.get('raw', 0):>5} {record.get('new', 0):>5}")

    print("\n当前数据底盘（来自归档）：")
    print(f"  独立岗位 {len(known_ids)} 条，覆盖城市 {len(city_counts)} / {city_count} 座"
          f"（{(len(city_counts) / max(city_count, 1)) * 100:.0f}%）")
    top = ", ".join(f"{c}:{n}" for c, n in city_counts.most_common(5))
    print(f"  城市 Top5：{top}")
    covered = sorted(city_counts.keys(), key=lambda c: city_counts[c])
    print(f"  覆盖最少的 8 座城市：{', '.join(f'{c}:{city_counts[c]}' for c in covered[:8])}")
    fam_top = ", ".join(
        f"{FAMILY_BY_ID.get(f, {}).get('name', f)}:{n}" for f, n in family_counts.most_common(6)
    )
    print(f"  工种 Top6：{fam_top}")
    print(f"\n下一步：py smart_crawl.py --plan  （查看会优先抓哪些组合）")


def build_report(ledger: dict, cities: list[str], family_ids: list[str],
                 city_counts: Counter, family_counts: Counter, city_family_counts: Counter,
                 picks: list[dict]) -> str:
    """生成 Markdown 覆盖率报告：城市 × 工种族矩阵 + 组合账本摘要。"""
    lines: list[str] = []
    lines.append("# 抓取覆盖率报告（smart_crawl）")
    lines.append("")
    lines.append(f"- 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"- 城市总数：{len(cities)}    工种族数：{len(family_ids)}")
    lines.append(f"- 独立岗位：{sum(city_counts.values())} 条，"
                 f"覆盖城市 {len(city_counts)} / {len(cities)} 座")
    combos = ledger.get("combos", {})
    lines.append(f"- 已用组合：{sum(1 for v in combos.values() if v.get('status') == 'done')} 个，"
                 f"零新增拉黑：{sum(1 for v in combos.values() if v.get('status') == 'saturated')} 个")
    lines.append("")

    lines.append("## 一、城市覆盖排行榜（缺口从大到小）")
    lines.append("")
    lines.append("| 排名 | 城市 | 已有岗位 | 状态 |")
    lines.append("| --- | --- | --- | --- |")
    ranked = sorted(cities, key=lambda c: (city_counts.get(c, 0), c))
    for index, city in enumerate(ranked[:80], 1):
        count = city_counts.get(city, 0)
        state = "空白" if count == 0 else ("薄弱" if count < 20 else "已覆盖")
        lines.append(f"| {index} | {display_city(city)} | {count} | {state} |")
    blank = [c for c in cities if city_counts.get(c, 0) == 0]
    lines.append("")
    lines.append(f"**完全空白城市 {len(blank)} 座**：{'、'.join(blank[:60])}"
                 + ("…" if len(blank) > 60 else ""))
    lines.append("")

    lines.append("## 二、工种覆盖情况")
    lines.append("")
    lines.append("| 工种族 | 代表关键词 | 已有岗位 | 已跑组合 |")
    lines.append("| --- | --- | --- | --- |")
    for fid in family_ids:
        info = FAMILY_BY_ID[fid]
        ran = sum(1 for k, v in combos.items()
                  if parse_combo_key(k)[1] == fid and v.get("status") == "done")
        lines.append(f"| {info['name']} | {' / '.join(info['keywords'][:3])} | "
                     f"{family_counts.get(fid, 0)} | {ran} |")
    lines.append("")

    lines.append("## 三、本次/下次调度计划（Top 60）")
    lines.append("")
    lines.append("| # | 城市 | 工种族 | 关键词 | 得分 | 依据 |")
    lines.append("| --- | --- | --- | --- | --- | --- |")
    for index, pick in enumerate(picks[:60], 1):
        lines.append(f"| {index} | {display_city(pick['city'])} | {pick['family_name']} | "
                     f"{pick['keyword']} | {pick['score']:.4f} | {pick['reason']} |")
    lines.append("")

    lines.append("## 四、已用组合清单（下次自动跳过）")
    lines.append("")
    lines.append("| 城市 | 工种族 | 关键词 | 切片 | 抓取条数 | 新增 | 重复率 | 时间 |")
    lines.append("| --- | --- | --- | --- | --- | --- | --- | --- |")
    for key, record in sorted(combos.items(), key=lambda kv: kv[1].get("run_at") or ""):
        city, fid, slice_id = parse_combo_key(key)
        name = FAMILY_BY_ID.get(fid, {}).get("name", fid)
        raw = record.get("raw", 0) or 0
        dup_rate = f"{(record.get('dup', 0) / raw * 100):.0f}%" if raw else "—"
        lines.append(f"| {display_city(city)} | {name} | {record.get('keyword', '')} | "
                     f"{SLICE_LABEL.get(slice_id, slice_id or '—')} | {raw} | "
                     f"{record.get('new', 0)} | {dup_rate} | {record.get('run_at', '')} |")
    lines.append("")
    return "\n".join(lines)


# ============================================================
# 九、主流程
# ============================================================
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="覆盖度驱动的智能抓取调度器（城市 × 工种族）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="示例：\n"
               "  py smart_crawl.py --plan\n"
               "  py smart_crawl.py --run --budget 20\n"
               "  py smart_crawl.py --report\n",
    )
    parser.add_argument("--plan", action="store_true", help="只输出调度计划，不抓取")
    parser.add_argument("--run", action="store_true", help="执行抓取")
    parser.add_argument("--status", action="store_true", help="查看账本与覆盖率")
    parser.add_argument("--report", action="store_true", help="生成覆盖率报告 Markdown")
    parser.add_argument("--reset", action="store_true", help="清空账本后退出")
    parser.add_argument("--check-env", action="store_true", help="检查依赖 / CDP / 登录态")

    parser.add_argument("--budget", type=int, default=20, help="本次最多抓多少个组合（默认 20）")
    parser.add_argument("--pages", type=int, default=5, help="每个组合抓多少页（上限 10，默认 5）")
    parser.add_argument("--strategy", choices=sorted(STRATEGIES), default="balanced",
                        help="调度策略：balanced 均衡 / breadth 铺广度 / depth 加深重点城市")
    parser.add_argument("--total-target", type=int, default=None,
                        help="岗位总量目标，决定各城市/工种的配额分母")
    parser.add_argument("--max-families-per-city", type=int, default=None,
                        help="同一座城市最多铺几个工种族")
    parser.add_argument("--max-runs-per-combo", type=int, default=1,
                        help="同一「城市|工种族」组合最多重复抓几次（默认 1，即不重复）")
    parser.add_argument("--combo-min-jobs", type=int, default=8,
                        help="归档中某「城市|工种族」已有多少条就视为已覆盖、不再抓（默认 8）")
    parser.add_argument("--cities", help="限定城市，逗号分隔（默认全部）")
    parser.add_argument("--exclude-cities", help="排除城市，逗号分隔（默认排除三沙/东沙群岛）")
    parser.add_argument("--include-all-cities", action="store_true",
                        help="不排除任何城市（默认会跳过三沙、东沙群岛这类空跑城市）")
    parser.add_argument("--families", help="限定工种族，逗号分隔 id 或中文名（默认全部）")
    parser.add_argument("--include-nationwide", action="store_true",
                        help="把「全国」也当成一个可抓的搜索范围。"
                             "铺城市覆盖时强烈建议开启：实测 1 次请求覆盖 28 座城市，"
                             "单城模式只能覆盖 1 座")
    parser.add_argument("--max-slices", type=int, default=0,
                        help="某个「城市×族」抓满页数上限时，最多再补几个维度切片的子查询"
                             "（0=关闭，默认）。BOSS 单查询上限 150 条，"
                             "切片能把它抬到上千条")
    parser.add_argument("--slice-strategy", choices=sorted(SLICE_PRESETS), default="experience",
                        help="切片维度：experience 按经验 / degree 按学历")
    parser.add_argument("--cdp-port", type=int, default=9222)
    parser.add_argument("--browser", choices=["edge", "chrome"], default="edge")
    parser.add_argument("--python", default=sys.executable, help="运行抓取脚本的解释器")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT), help="导入后的 CSV 路径")
    parser.add_argument("--import-every", type=int, default=5, help="每多少个组合导入一次")
    parser.add_argument("--delay", type=float, default=None,
                        help="组合之间的等待秒数（默认随机 20-45 秒）")
    parser.add_argument("--seed", type=int, default=20260918, help="随机种子，保证计划可复现")
    parser.add_argument("--scraper-script", default=None,
                        help="覆盖抓取脚本路径（仅调试用，正常不需要指定）")
    parser.add_argument("--skip-browser-check", action="store_true",
                        help="跳过浏览器/CDP 检查（仅调试用）")

    args = parser.parse_args(argv)
    random.seed(args.seed)

    if args.scraper_script:
        global SCRAPER_SCRIPT
        SCRAPER_SCRIPT = Path(args.scraper_script).resolve()

    # ---------- 重置 ----------
    if args.reset:
        save_ledger(empty_ledger())
        print(f"账本已清空：{LEDGER_FILE}")
        return 0

    # ---------- 读取城市与工种族 ----------
    city_codes = load_city_codes(include_nationwide=args.include_nationwide)
    all_cities = list(city_codes.keys())
    if args.cities:
        want = {c.strip() for c in args.cities.split(",") if c.strip()}
        cities = [c for c in all_cities if c in want]
        missing = want - set(cities)
        if missing:
            print(f"[警告] 城市码表里没有这些城市，已忽略：{'、'.join(sorted(missing))}")
    else:
        cities = all_cities

    # 空跑城市黑名单（可在命令行覆盖）
    if args.include_all_cities:
        excluded: set[str] = set()
    elif args.exclude_cities is not None:
        excluded = {c.strip() for c in args.exclude_cities.split(",") if c.strip()}
    else:
        excluded = set(DEFAULT_EXCLUDE_CITIES)
    if excluded:
        cities = [c for c in cities if c not in excluded]

    family_ids = [fid for fid, _, _, _ in JOB_FAMILIES]
    if args.families:
        want = {f.strip() for f in args.families.split(",") if f.strip()}
        family_ids = [fid for fid in family_ids
                      if fid in want or FAMILY_BY_ID[fid]["name"] in want]
    if not cities or not family_ids:
        print("[错误] 城市或工种族为空，无法调度", file=sys.stderr)
        return 1

    # ---------- 策略参数 ----------
    preset = STRATEGIES[args.strategy]
    total_target = args.total_target or preset["total_target"]
    max_families = args.max_families_per_city or preset["max_families_per_city"]
    zero_bonus = preset["zero_bonus"]
    # 「全国」的优先倍数由策略决定：铺广度时让它霸榜，深耕时完全不碰
    nationwide_factor = NATIONWIDE_FACTOR.get(args.strategy, 1.0)
    slices = SLICE_PRESETS[args.slice_strategy] if args.max_slices > 0 else []

    # ---------- 盘点底座 ----------
    print("正在扫描归档数据，还原城市/工种覆盖情况…", flush=True)
    known_ids, city_counts, family_counts, city_family_counts = scan_archive()
    ledger = load_ledger()
    ledger_done = ledger_city_family_counts(ledger)

    # depth 策略：只在「已经有数据的城市」里深耕，跳过空白城市
    if args.strategy == "depth":
        cities = [c for c in cities if city_counts.get(c, 0) > 0] or cities

    cfg = {
        "strategy": args.strategy,
        "budget": args.budget,
        "pages": args.pages,
        "total_target": total_target,
        "max_families_per_city": max_families,
        "combo_min_jobs": args.combo_min_jobs,
        "max_slices": args.max_slices,
        "slice_strategy": args.slice_strategy,
        "nationwide_factor": nationwide_factor,
        "cities": len(cities),
        "families": len(family_ids),
    }

    scheduler = Scheduler(
        cities, [FAMILY_BY_ID[f] for f in family_ids], ledger,
        city_counts, family_counts, ledger_done,
        total_target=total_target,
        max_families_per_city=max_families,
        zero_bonus=zero_bonus,
        cooldown_city=3,            # 最近 3 个组合抓过的城市，降权
        cooldown_family=2,          # 最近 2 个组合抓过的族，降权
        max_runs_per_combo=args.max_runs_per_combo,
        combo_min_jobs=args.combo_min_jobs,
        city_family_covered=city_family_counts,   # 归档里已覆盖的城市×族，直接跳过
        nationwide_factor=nationwide_factor,
        max_slices=args.max_slices,
    )

    # ---------- 只读类命令 ----------
    if args.status:
        print_status(ledger, known_ids, city_counts, family_counts, len(cities), args.strategy)
        return 0

    if args.check_env:
        print(f"归档目录 : {ARCHIVE_DIR}  ({'存在' if ARCHIVE_DIR.exists() else '不存在'})")
        print(f"账本文件 : {LEDGER_FILE}  ({'存在' if LEDGER_FILE.exists() else '待创建'})")
        print(f"抓取脚本 : {SCRAPER_SCRIPT}  ({'存在' if SCRAPER_SCRIPT.exists() else '缺失'})")
        print(f"城市码表 : {CITY_CODES_FILE}  ({len(city_codes)} 个城市)")
        print(f"解释器   : {args.python}")
        if not cdp_alive(args.cdp_port):
            print("浏览器   : 未运行（首次抓取会自动启动并等待登录）")
        else:
            print("浏览器   : CDP 已就绪")
        print("\n运行抓取脚本自检：")
        return run_command([args.python, str(SCRAPER_SCRIPT), "--check",
                            "--cdp-port", str(args.cdp_port)])

    # ---------- 生成计划 ----------
    picks = scheduler.plan(args.budget)
    write_plan_file(picks, cfg)

    if args.report:
        report = build_report(ledger, cities, family_ids, city_counts,
                              family_counts, city_family_counts, picks)
        REPORT_FILE.parent.mkdir(parents=True, exist_ok=True)
        REPORT_FILE.write_text(report, encoding="utf-8")
        print(f"报告已生成：{REPORT_FILE}")
        if not args.plan and not args.run:
            return 0

    if args.plan and not args.run:
        print("=" * 72)
        print(f"  调度计划（策略 {args.strategy}，共 {len(picks)} 个组合，pages={args.pages}）")
        print("=" * 72)
        print(f"  城市 {len(cities)} 座 × 工种族 {len(family_ids)} 个"
              f" = {len(cities) * len(family_ids)} 个可能的组合")
        print(f"  岗位总量目标 {total_target}，同城最多铺 {max_families} 个族，"
              f"已用组合 {len(ledger_done)} 个（自动跳过）")
        print()
        print_plan(picks)
        print(f"\n完整计划已写入：{PLAN_FILE}")
        print("确认后执行：py smart_crawl.py --run --budget %d" % len(picks))
        return 0

    if not args.run:
        print("未指定动作。可选：--plan / --run / --status / --report / --check-env")
        return 0

    # ---------- 正式抓取 ----------
    if not picks:
        print("没有待抓取的组合，任务已收敛。")
        do_import(args.python, args.output)
        return 0

    print("=" * 72)
    print(f"  开始抓取 {len(picks)} 个组合（策略 {args.strategy}，每组合 {args.pages} 页）")
    print("=" * 72)
    print_plan(picks, limit=len(picks))

    python = args.python
    if not args.skip_browser_check and not cdp_alive(args.cdp_port):
        if not start_browser(python, args.cdp_port, args.browser):
            print("[错误] 浏览器不可用，已停止。请先按上面的提示用后台方式启动浏览器并登录。",
                  file=sys.stderr)
            return 1
    elif args.skip_browser_check:
        print("[调试] 已跳过浏览器/CDP 检查\n")

    batch_dir = ARCHIVE_DIR / f"smart_{datetime.now().strftime('%Y-%m-%d_%H%M')}"
    batch_dir.mkdir(parents=True, exist_ok=True)
    print(f"\n本次归档目录：{batch_dir}\n")

    started = time.time()
    consecutive_failures = 0
    combo_records: list[dict] = []

    page_cap = args.pages * 15          # BOSS 每页 15 条，抓满页数说明查询被上限截断

    def run_one_query(label, out_file, extra_args, keyword, city):
        """跑一次抓取并返回 (退出码, 原始条数, 新增 job_id 集合, 是否触顶)。"""
        code = run_command([
            python, str(SCRAPER_SCRIPT),
            "--keyword", keyword,
            "--city", city,
            "--pages", str(args.pages),
            "--no-detail",
            "--format", "json",
            "--output", str(out_file),
            "--cdp-port", str(args.cdp_port),
        ] + extra_args)
        if not out_file.exists():
            return code, 0, set(), False
        file_ids, raw = read_job_ids(out_file)
        return code, raw, file_ids - known_ids, raw >= page_cap

    for index, pick in enumerate(picks, 1):
        city, fid = pick["city"], pick["family_id"]
        key = family_key(city, fid)
        skip_base = pick.get("skip_base")
        head = f"\n===== [{index}/{len(picks)}] {pick['family_name']} @ {display_city(city)}"
        if skip_base:
            head += "（基础查询已抓过，本次只补切片）"
        print(head + f"  关键词「{pick['keyword']}」 =====", flush=True)

        total_raw = 0
        all_new: set[str] = set()
        base_truncated = False
        last_code = 0

        # ---- 第 1 步：基础查询（不带筛选）----
        if not skip_base:
            out_file = archive_path_for(batch_dir, city, fid)
            code, raw, new_ids, truncated = run_one_query(
                "base", out_file, [], pick["keyword"], city)
            last_code = code
            total_raw += raw
            all_new |= new_ids
            base_truncated = truncated
            record = ledger["combos"].get(key, {})
            record.update({
                "city": city, "family_id": fid, "family_name": pick["family_name"],
                "keyword": pick["keyword"], "slice_id": "",
                "status": "failed" if code != 0 else ("saturated" if not new_ids else "done"),
                "raw": raw, "new": len(new_ids), "dup": max(0, raw - len(new_ids)),
                "runs": int(record.get("runs", 0)) + 1,
                "run_at": datetime.now().isoformat(timespec="seconds"),
                "exit_code": code,
            })
            ledger["combos"][key] = record
            if code == 0:
                known_ids |= new_ids
                consecutive_failures = 0
                if new_ids:
                    print(f"  ✅ 基础查询 {raw} 条，新增 {len(new_ids)} 条"
                          f"（重复 {raw - len(new_ids)} 条）", flush=True)
                else:
                    print(f"  ⚠️ 基础查询 {raw} 条全部是已有岗位 → 标记 saturated", flush=True)
            else:
                consecutive_failures += 1
                print(f"  ❌ 基础查询失败（exit={code}）", flush=True)
        else:
            # 基础查询已完成过，用最近一次记录里的触顶信息决定是否继续切片
            prev = ledger["combos"].get(key, {})
            base_truncated = bool(prev.get("truncated"))
        save_ledger(ledger)

        # ---- 第 2 步：维度切片（只在「确实被上限截断」时才做，避免浪费请求）----
        if slices and last_code == 0 and base_truncated:
            used = {parse_combo_key(k)[2] for k, v in ledger["combos"].items()
                    if family_key(*parse_combo_key(k)[:2]) == key
                    and parse_combo_key(k)[2] and v.get("status") in ("done", "saturated")}
            todo = [s for s in slices if s[0] not in used][: args.max_slices]
            if todo:
                print(f"  ↳ 查询触顶（{page_cap} 条），继续补 {len(todo)} 个"
                      f"{'经验' if args.slice_strategy == 'experience' else '学历'}切片", flush=True)
            for sid, label, extra in todo:
                slice_key = combo_key(city, fid, sid)
                out_file = archive_path_for(batch_dir, city, f"{fid}_{sid}")
                print(f"    · 切片【{label}】", flush=True)
                code, raw, new_ids, _ = run_one_query(
                    label, out_file, extra, pick["keyword"], city)
                if code != 0:
                    consecutive_failures += 1
                else:
                    consecutive_failures = 0
                total_raw += raw
                all_new |= new_ids
                known_ids |= new_ids
                srec = ledger["combos"].get(slice_key, {})
                srec.update({
                    "city": city, "family_id": fid, "family_name": pick["family_name"],
                    "keyword": pick["keyword"], "slice_id": sid, "slice_label": label,
                    "status": "failed" if code != 0 else ("saturated" if not new_ids else "done"),
                    "raw": raw, "new": len(new_ids), "dup": max(0, raw - len(new_ids)),
                    "runs": int(srec.get("runs", 0)) + 1,
                    "run_at": datetime.now().isoformat(timespec="seconds"),
                    "exit_code": code,
                })
                ledger["combos"][slice_key] = srec
                save_ledger(ledger)
                print(f"      {'✅' if new_ids else '⚠️'} {raw} 条，新增 {len(new_ids)} 条",
                      flush=True)
                time.sleep(random.uniform(10, 20))
                if consecutive_failures >= 3:
                    break

        # 把「基础查询是否触顶」记下来，下次续跑切片时用
        base_rec = ledger["combos"].get(key)
        if base_rec is not None and not skip_base:
            base_rec["truncated"] = base_truncated
            save_ledger(ledger)

        if total_raw:
            print(f"  ▸ 本组合合计 {total_raw} 条，新增不重复岗位 {len(all_new)} 条", flush=True)
        combo_records.append({
            "city": city, "family_id": fid, "raw": total_raw, "new": len(all_new),
        })
        save_ledger(ledger)

        if index % args.import_every == 0:
            print("\n--- 阶段性导入 ---")
            do_import(python, args.output)

        if consecutive_failures >= 3:
            print("\n连续失败 3 次，可能是风控或登录态失效，提前停止本次任务。")
            print("排查：py smart_crawl.py --check-env")
            break

        if index < len(picks):
            gap = args.delay if args.delay is not None else random.uniform(20, 45)
            print(f"  组合间等待 {gap:.0f}s…", flush=True)
            time.sleep(gap)

    ledger["runs"].append({
        "at": datetime.now().isoformat(timespec="seconds"),
        "strategy": args.strategy,
        "budget": len(picks),
        "executed": len(combo_records),
        "new_jobs": sum(r.get("new", 0) for r in combo_records),
        "raw": sum(r.get("raw", 0) for r in combo_records),
    })
    save_ledger(ledger)

    print("\n--- 最终导入 ---")
    do_import(python, args.output)

    elapsed = (time.time() - started) / 60
    total_new = sum(r.get("new", 0) for r in combo_records)
    total_raw = sum(r.get("raw", 0) for r in combo_records)
    print("\n" + "=" * 72)
    print(f"  本次完成 {len(combo_records)} 个组合，耗时 {elapsed:.1f} 分钟")
    print(f"  抓取 {total_raw} 条 → 新增不重复岗位 {total_new} 条"
          f"（收益率 {total_new / max(total_raw, 1) * 100:.0f}%）")
    print(f"  账本累计已用组合 {len(ledger['combos'])} 个，下次运行自动跳过")
    print(f"  数据文件：{args.output}")
    print(f"  覆盖率报告：py smart_crawl.py --report")
    print("=" * 72)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
