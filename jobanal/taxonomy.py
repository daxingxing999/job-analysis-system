"""工种族定义的唯一来源（词表 + 排序 + 分类规则）。

以前这份词表**分散在两处**：``JOB_FAMILIES``（族定义与搜索关键词）与
``FAMILY_MATCH_TERMS`` / ``FAMILY_MATCH_ORDER``（识别词表与优先级），
必须手工保持同步——而它们已经失同步了：识别词表里有 ``mobile``，
族定义里没有，于是 ``family_of_title()`` 会返回一个不存在的族 id，
还会让 ``city_family_covered["某城|mobile"]`` 给该城虚增一个「已铺族」，
提前触发「该城族数已铺满」而跳过真正该抓的组合（幽灵族问题）。

本模块**只用标准库**。

约定
----
* ``JOB_FAMILIES`` 是族的事实来源：id / 名称 / 搜索关键词 / 体量权重。
* ``FAMILY_MATCH_TERMS`` 只能包含 ``JOB_FAMILIES`` 里存在的 id（有测试强制）。
* 分类时**先比具体族、再比泛化族**（见 ``FAMILY_MATCH_ORDER``）：
  「测试开发工程师」「移动端开发工程师」应该归到 test / mobile-adjacent，
  而不是被 backend 的泛词「开发」吸走。
"""

from __future__ import annotations

# ============================================================
# 一、工种族定义（21 个族）
#     weight = 该族在全国岗位池里的体量权重，用于「冷城市 × 热工种」配对。
# ============================================================
JOB_FAMILIES: list[tuple[str, str, list[str], float]] = [
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
    # 体量小但是真实存在的独立族：实测全库 Android/iOS/移动端 标题约十余条。
    # 以前只在识别词表里有 mobile、族定义里没有，才成了幽灵族。
    ("mobile",     "移动端",     ["Android", "iOS", "移动端开发", "Flutter"],                  0.35),
]

FAMILY_BY_ID = {
    fid: {"id": fid, "name": name, "keywords": list(keywords), "weight": weight}
    for fid, name, keywords, weight in JOB_FAMILIES
}
FAMILY_NAME = {fid: name for fid, name, _, _ in JOB_FAMILIES}
FAMILY_IDS = [fid for fid, _, _, _ in JOB_FAMILIES]


# 工种族识别词表：只用于「统计已有岗位属于哪个工种」，不参与抓取。
# 比 JOB_FAMILIES 的搜索关键词宽得多，因为岗位标题是长尾的（实测 3739 种标题）。
#
# 注意：这里的 id 必须都在 JOB_FAMILIES 里（tests/test_classify.py 会强制检查）。
# 曾经的 "mobile" 就是反例：词表有、族定义没有，成了幽灵族。
FAMILY_MATCH_TERMS: dict[str, list[str]] = {
    "backend":   ["java", "python", "golang", "php", "c++", "c#", "node", "后端", "服务端",
                  "软件开发", "研发工程师", "架构师", "开发工程师", "开发"],
    "frontend":  ["前端", "web开发", "vue", "react", "html", "小程序", "h5", "页面"],
    "mobile":    ["android", "ios", "移动端", "flutter", "客户端开发", "安卓"],
    "ai":        ["算法", "机器学习", "深度学习", "大模型", "人工智能", "nlp", "计算机视觉",
                  "推荐算法", "数据挖掘", "ai工程"],
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
    "hr":        ["人力", "人事", "招聘", "行政", "薪酬", "培训专员"],
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

# 匹配顺序：越靠前越优先。
# 关键点（这是与旧实现最大的差别）：把**特征强的具体族**排在 backend 之前。
# 旧顺序把 backend 放第一，而 backend 的词表里有泛词「开发」，
# 于是「测试开发工程师」「移动端开发工程师」「嵌入式开发工程师」全被吸进 backend。
FAMILY_MATCH_ORDER: list[str] = [
    "frontend", "mobile", "test", "data", "ai", "ops", "security", "design",
    "product", "backend", "operation", "marketing", "sales", "medical", "edu",
    "hr", "finance", "mech", "construct", "logistics", "service",
]

# 泛化兜底词：命中它只算「弱证据」。当标题里同时出现具体族的强词时，
# 泛化词让位（例如「测试开发工程师」→ test 而不是 backend）。
#
# 「培训」也在这里：招聘广告里「带薪培训」「可晋升+培训」极常见，
# 但那是福利描述，不代表这是教育岗。实测校准前它把「物流专员（可晋升+带薪培训）」
# 「销售岗（无责5000+培训）」都吸进了教育族。
FAMILY_WEAK_TERMS = {
    "开发", "开发工程师", "软件开发", "设计", "运营", "市场", "销售", "培训",
}


def validate_taxonomy() -> list[str]:
    """自检词表一致性，返回问题列表（空表示健康）。"""
    problems = []
    declared = set(FAMILY_IDS)
    for fid in FAMILY_MATCH_TERMS:
        if fid not in declared:
            problems.append(f"幽灵族：词表里的 {fid!r} 不在 JOB_FAMILIES 中")
    for fid in FAMILY_MATCH_ORDER:
        if fid not in declared:
            problems.append(f"排序表里的 {fid!r} 不在 JOB_FAMILIES 中")
    for fid in FAMILY_MATCH_TERMS:
        if fid not in FAMILY_MATCH_ORDER:
            problems.append(f"词表里的 {fid!r} 没有出现在 FAMILY_MATCH_ORDER 中")
    for fid in declared:
        if fid not in FAMILY_MATCH_TERMS:
            problems.append(f"族 {fid!r} 没有识别词表，永远不会被分类命中")
    return problems
